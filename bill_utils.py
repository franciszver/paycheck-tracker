import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
import holidays

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "transactions.db"

with open(BASE_DIR / "config.json") as f:
    _config = json.load(f)


def payday_anchor():
    return date.fromisoformat(_config["paycheck"]["last_friday"])


def nth_payday_of_month(year, month, n):
    anchor = payday_anchor()
    us_holidays = holidays.UnitedStates(years=[year])
    start = date(year, month, 1)
    p = anchor
    while p > start:
        p -= timedelta(days=14)
    while p < start:
        p += timedelta(days=14)
    count = 0
    while p.month == month and p.year == year:
        adjusted = p - timedelta(days=1) if p in us_holidays else p
        count += 1
        if count == n:
            return adjusted
        p += timedelta(days=14)
    return None


def _load_date_overrides(year, month):
    if not DB_PATH.exists():
        return {}
    month_str = f"{year:04d}-{month:02d}"
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT bill_name, month, new_day FROM date_overrides "
                "WHERE month=? OR month='permanent'",
                (month_str,),
            ).fetchall()
        finally:
            conn.close()
        result = {}
        for bill_name, m, new_day in rows:
            if m == month_str:
                result[bill_name] = (new_day, True)
            elif bill_name not in result:
                result[bill_name] = (new_day, False)
        return result
    except sqlite3.OperationalError:
        return {}


_overrides_cache = {"key": None, "data": {}}
_amount_overrides_cache = {"key": None, "data": {}}


def _get_date_override(bill_name, year, month):
    key = (year, month)
    if _overrides_cache["key"] != key:
        _overrides_cache["key"] = key
        _overrides_cache["data"] = _load_date_overrides(year, month)
    entry = _overrides_cache["data"].get(bill_name)
    if entry is None:
        return None, False
    return entry


def invalidate_date_override_cache():
    """Force the next resolve_due_date call to re-read from the DB.

    Call this after any write to the date_overrides table so that
    in-process callers (e.g. the Discord bot) don't serve a stale due date.
    """
    _overrides_cache["key"] = None
    _overrides_cache["data"] = {}


def _load_amount_overrides(year, month):
    if not DB_PATH.exists():
        return {}
    month_str = f"{year:04d}-{month:02d}"
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT bill_name, month, new_amount FROM amount_overrides "
                "WHERE month=? OR month='permanent'",
                (month_str,),
            ).fetchall()
        finally:
            conn.close()
        result = {}
        for bill_name, m, new_amount in rows:
            if m == month_str:
                result[bill_name] = new_amount
            elif bill_name not in result:
                result[bill_name] = new_amount
        return result
    except sqlite3.OperationalError:
        return {}


def _get_amount_override(bill_name, year, month):
    key = (year, month)
    if _amount_overrides_cache["key"] != key:
        _amount_overrides_cache["key"] = key
        _amount_overrides_cache["data"] = _load_amount_overrides(year, month)
    return _amount_overrides_cache["data"].get(bill_name)


def invalidate_amount_override_cache():
    """Force the next resolve_amount call to re-read from the DB.

    Call this after any write to the amount_overrides table so that
    in-process callers (e.g. the Discord bot) don't serve stale data.
    """
    _amount_overrides_cache["key"] = None
    _amount_overrides_cache["data"] = {}


def resolve_amount(b, today):
    override = _get_amount_override(b.get("name", ""), today.year, today.month)
    return override if override is not None else b["amount"]


def resolve_due_date(b, today):
    name = b.get("name", "")
    override_day, is_month_specific = _get_date_override(name, today.year, today.month)

    if override_day is not None:
        eom = end_of_month(date(today.year, today.month, 1)).day
        day = min(override_day, eom)
        due_date = date(today.year, today.month, day)
        if due_date < today and not is_month_specific:
            # Permanent overrides roll to next month once passed. A "this month
            # only" override has nothing to roll forward to — return it as-is
            # so callers correctly see it as already past rather than silently
            # falling back to the bill's un-overridden schedule.
            nm = today.month + 1 if today.month < 12 else 1
            ny = today.year if today.month < 12 else today.year + 1
            eom_next = end_of_month(date(ny, nm, 1)).day
            due_date = date(ny, nm, min(override_day, eom_next))
        return due_date

    trigger = b.get("trigger")
    if trigger in ("1st_paycheck", "2nd_paycheck"):
        n = 1 if trigger == "1st_paycheck" else 2
        due = nth_payday_of_month(today.year, today.month, n)
        if due is None or due < today:
            nm = today.month + 1 if today.month < 12 else 1
            ny = today.year if today.month < 12 else today.year + 1
            due = nth_payday_of_month(ny, nm, n)
        return due
    due_date = date(today.year, today.month, b["day_of_month"])
    if due_date < today:
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year if today.month < 12 else today.year + 1
        due_date = date(ny, nm, b["day_of_month"])
    return due_date


def end_of_month(d):
    nm = d.month + 1 if d.month < 12 else 1
    ny = d.year if d.month < 12 else d.year + 1
    return date(ny, nm, 1) - timedelta(days=1)


def _is_income(b):
    return b.get("type") == "income"


def find_bill_payment(name, amount, txns, keywords=None, min_amount=None, tolerance_pct=None):
    """Return the first txn matching this bill, or None.

    keywords: flat list -> one AND-group; list-of-lists -> OR of AND-groups.
    min_amount: optional floor on the transaction amount, to avoid matching
    small unrelated charges that happen to share a broad keyword.
    tolerance_pct: override the default ±10% tolerance (e.g. 0.6 for variable utility bills).
    """
    tol = tolerance_pct if tolerance_pct is not None else 0.10
    tolerance = amount * tol

    def amt_ok(t):
        a = abs(t["amount"])
        if min_amount is not None and a < min_amount:
            return False
        return abs(a - amount) <= tolerance

    if keywords:
        groups = keywords if isinstance(keywords[0], list) else [keywords]
        for t in txns:
            tn = (t["name"] or "").lower()
            tm = (t["merchant_name"] or "").lower()
            if amt_ok(t) and any(all(kw in tn or kw in tm for kw in g) for g in groups):
                return t
        return None

    keyword = re.sub(r"\s+\(.*\)", "", name).split()[0].lower()
    full = name.lower()
    for t in txns:
        tn, tm = (t["name"] or "").lower(), (t["merchant_name"] or "").lower()
        name_match = (keyword in tn or keyword in tm) or (full in tn or full in tm)
        if name_match and amt_ok(t):
            return t
    return None


def bill_is_paid(name, amount, txns, keywords=None, min_amount=None, tolerance_pct=None):
    """True if a transaction matches this bill (keyword/name + amount ±10%, optional floor)."""
    return find_bill_payment(name, amount, txns, keywords, min_amount, tolerance_pct) is not None


def bills_due_this_month(bill_list, start=None):
    today = date.today()
    ref = start if start is not None else today
    window = end_of_month(ref)
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or _is_income(b):
            continue
        due_date = resolve_due_date(b, ref)
        if due_date and ref <= due_date <= window:
            due.append((b["name"], resolve_amount(b, due_date), due_date, b.get("keywords"), b.get("min_amount"), b.get("tolerance_pct")))
    due.sort(key=lambda x: x[2])
    return due


def income_this_month(bill_list, start=None):
    today = date.today()
    ref = start if start is not None else today
    window = end_of_month(ref)
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or not _is_income(b):
            continue
        due_date = resolve_due_date(b, ref)
        if due_date and ref <= due_date <= window:
            due.append((b["name"], resolve_amount(b, due_date), due_date, b.get("keywords"), b.get("min_amount"), b.get("tolerance_pct")))
    due.sort(key=lambda x: x[2])
    return due


def _txn_effective_date(txn):
    """Return the actual transaction date, not the posting date.

    Uses authorized_date when Plaid provides it.  Falls back to parsing BoA's
    'CHECKCARD MMDD' / 'PURCHASE MMDD' naming convention, where the four-digit
    code after the keyword is the authorization date (month + day).
    """
    if txn.get("authorized_date"):
        return txn["authorized_date"]
    m = re.search(r"(?:CHECKCARD|PURCHASE) (\d{2})(\d{2})\b", txn.get("name") or "")
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        posted = date.fromisoformat(txn["date"])
        # If the encoded month is after the posted month, it must be the prior year.
        yr = posted.year if mo <= posted.month else posted.year - 1
        try:
            return str(date(yr, mo, dy))
        except ValueError:
            pass
    return txn["date"]


def get_manual_paid_names(month_str, conn=None):
    """Bill/income names manually marked paid for the given month (manual_payments table).

    Pass an existing sqlite3 connection via `conn` to reuse it; otherwise a
    short-lived connection is opened and closed here.
    """
    owns_conn = conn is None
    if owns_conn:
        if not DB_PATH.exists():
            return set()
        conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT bill_name FROM manual_payments WHERE month=?", (month_str,)
    ).fetchall()
    if owns_conn:
        conn.close()
    return {r[0] for r in rows}


def get_ignored_bill_names(month_str, conn=None):
    """Bill/income names ignored for the given month (ignored_bills table).

    Pass an existing sqlite3 connection via `conn` to reuse it; otherwise a
    short-lived connection is opened and closed here.
    """
    owns_conn = conn is None
    if owns_conn:
        if not DB_PATH.exists():
            return set()
        conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT bill_name FROM ignored_bills WHERE month=?", (month_str,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if owns_conn:
        conn.close()
    return {r[0] for r in rows}


def load_one_time_items(item_type=None, month_str=None, start=None):
    """Load one-time items from the DB for a given month.

    Returns tuples matching the bills_due_this_month shape:
    (name, amount, due_date, None, None, None).
    """
    if not DB_PATH.exists():
        return []
    today = date.today()
    if month_str is None:
        month_str = today.strftime("%Y-%m")
    year, mo = int(month_str[:4]), int(month_str[5:7])
    ref = start if start is not None else today

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    query = "SELECT name, amount, day_of_month, type FROM one_time_items WHERE month=?"
    params = [month_str]
    if item_type:
        query += " AND type=?"
        params.append(item_type)
    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()

    eom = end_of_month(date(year, mo, 1)).day
    result = []
    for r in rows:
        dom = min(r["day_of_month"], eom)
        due_date = date(year, mo, dom)
        if due_date >= ref:
            result.append((r["name"], r["amount"], due_date, None, None, None))
    result.sort(key=lambda x: x[2])
    return result
