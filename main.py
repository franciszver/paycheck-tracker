import json
import os
import smtplib
import sqlite3
import logging
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv
import holidays
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions
import plaid_client as pc
from bill_utils import (
    payday_anchor, resolve_due_date, resolve_amount,
    end_of_month, _is_income, bills_due_this_month, income_this_month,
    bill_is_paid, load_one_time_items, _txn_effective_date,
    get_manual_paid_names, get_ignored_bill_names,
)

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "transactions.db"
LOG_PATH = BASE_DIR / "logs" / "daily.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

with open(BASE_DIR / "config.json") as f:
    config = json.load(f)

with open(BASE_DIR / "bills.json") as f:
    bills = json.load(f)


def next_payday(last_friday_str):
    payday = date.fromisoformat(last_friday_str)
    today = date.today()
    while payday < today:
        payday += timedelta(days=14)
    us_holidays = holidays.UnitedStates(years=[payday.year])
    if payday in us_holidays:
        payday -= timedelta(days=1)
    return payday, (payday - today).days


def bills_due_before_payday(payday, bill_list):
    """Bills due before next paycheck — used for safe money calculation."""
    today = date.today()
    cutoff = payday + timedelta(days=14) if payday == today else payday
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or _is_income(b):
            continue
        if b.get("trigger") in ("1st_paycheck", "2nd_paycheck"):
            continue
        due_date = resolve_due_date(b, today)
        if due_date and today <= due_date < cutoff:
            due.append((b["name"], resolve_amount(b, due_date), due_date))
    due.sort(key=lambda x: x[2])
    return due


def income_before_payday(payday, bill_list):
    """Income expected before next paycheck — offsets bills in safe money calc."""
    today = date.today()
    cutoff = payday + timedelta(days=14) if payday == today else payday
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or not _is_income(b):
            continue
        due_date = resolve_due_date(b, today)
        if due_date and today <= due_date < cutoff:
            due.append((b["name"], resolve_amount(b, due_date), due_date))
    due.sort(key=lambda x: x[2])
    return due


def txns_near_due(txns, due_date, before=14, after=10):
    """Filter transactions to a window around the due date."""
    lo = due_date - timedelta(days=before)
    hi = due_date + timedelta(days=after)
    return [t for t in txns if lo <= date.fromisoformat(t["date"]) <= hi]


def load_month_transactions(month_start, month_end):
    """Fetch checking-account transactions for the month using effective transaction dates.

    Fetches 5 extra days before month_start to catch transactions authorized in the
    prior month but posted in the first few days of this month (e.g. a May 29 charge
    that posts June 1).  Each transaction's date is replaced with its effective date
    (authorized_date, CHECKCARD/PURCHASE MMDD, or posted date) before filtering.
    """
    if not DB_PATH.exists():
        return []
    fetch_start = month_start - timedelta(days=5)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT t.name, t.merchant_name, t.category_detailed,"
        "       t.date, t.authorized_date, t.amount"
        " FROM transactions t"
        " JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.subtype = 'checking'"
        "   AND t.date BETWEEN ? AND ?",
        (str(fetch_start), str(month_end)),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        txn = dict(r)
        eff = _txn_effective_date(txn)
        if month_start <= date.fromisoformat(eff) <= month_end:
            txn["date"] = eff
            result.append(txn)
    return result


def paycheck_is_received(pd, txns):
    """True if an INCOME_SALARY deposit lands within 2 days of the expected payday."""
    return any(
        t["category_detailed"] == "INCOME_SALARY"
        and t["amount"] < 0
        and abs((pd - date.fromisoformat(t["date"])).days) <= 2
        for t in txns
    )


def get_paycheck_net_amount():
    """Average net paycheck from the most recent INCOME_SALARY deposits in the DB."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT amount FROM transactions WHERE category_detailed='INCOME_SALARY'"
        " AND pending=0 ORDER BY date DESC LIMIT 4"
    ).fetchall()
    conn.close()
    if not rows:
        return None
    return round(abs(sum(r["amount"] for r in rows) / len(rows)), 2)


def paychecks_in_window(today, window_end):
    """Return all payday dates in [today, window_end]."""
    anchor = payday_anchor()
    us_holidays = holidays.UnitedStates(years={today.year, window_end.year})
    p = anchor
    while p < today:
        p += timedelta(days=14)
    result = []
    while p <= window_end + timedelta(days=1):
        adjusted = p - timedelta(days=1) if p in us_holidays else p
        if today <= adjusted <= window_end:
            result.append(adjusted)
        p += timedelta(days=14)
    return result


def spending_suggestion(category_detailed):
    """Return (monthly_avg, per_period_avg) from full transaction history for a Plaid category."""
    if not DB_PATH.exists():
        return None, None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT MIN(date) as first_date, MAX(date) as last_date,"
        " ROUND(SUM(amount),2) as total"
        " FROM transactions"
        " WHERE amount>0 AND pending=0 AND category_detailed=?",
        (category_detailed,),
    ).fetchone()
    conn.close()
    if not row or not row["first_date"]:
        return None, None
    first = date.fromisoformat(row["first_date"])
    last = date.fromisoformat(row["last_date"])
    total_days = (last - first).days + 1
    if total_days < 14:
        return None, None
    return round(row["total"] * 30 / total_days), round(row["total"] * 14 / total_days)


def send_email(subject, body, config):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["gmail"]["address"]
    msg["To"] = ", ".join(config["recipients"])

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(config["gmail"]["address"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.sendmail(
            config["gmail"]["address"],
            config["recipients"],
            msg.as_string(),
        )


def main():
    client = pc.make_client(config)
    resp = client.accounts_balance_get(
        AccountsBalanceGetRequest(
            access_token=pc.access_token(config),
            options=AccountsBalanceGetRequestOptions(
                min_last_updated_datetime=datetime.now(timezone.utc),
            ),
        )
    )
    account = next(a for a in resp["accounts"] if str(a["subtype"]) == "checking")
    balance = account["balances"]["available"] or account["balances"]["current"]

    today = date.today()
    month_start = date(today.year, today.month, 1)
    window = end_of_month(today)

    payday, days_until = next_payday(config["paycheck"]["last_friday"])
    payday_bills = bills_due_before_payday(payday, bills)
    payday_income = income_before_payday(payday, bills)

    month_str = today.strftime("%Y-%m")
    ignored = get_ignored_bill_names(month_str)

    # One-time items due before next payday (manual-only detection)
    cutoff = payday + timedelta(days=14) if payday == today else payday
    ot_bills_safe = [b for b in load_one_time_items('bill') if b[2] < cutoff and b[0] not in ignored]
    ot_income_safe = [i for i in load_one_time_items('income') if i[2] < cutoff and i[0] not in ignored]

    total_due = sum(b[1] for b in payday_bills if b[0] not in ignored) + sum(b[1] for b in ot_bills_safe)
    total_income_safe = sum(i[1] for i in payday_income if i[0] not in ignored) + sum(i[1] for i in ot_income_safe)
    safe_money = balance - total_due + total_income_safe
    threshold = config.get("low_balance_threshold", 200)

    # Full-month data for the snapshot list (recurring + one-time)
    all_bills_month = bills_due_this_month(bills, start=month_start)
    all_income_month = income_this_month(bills, start=month_start)
    ot_bills_month = load_one_time_items('bill', start=month_start)
    ot_income_month = load_one_time_items('income', start=month_start)
    all_income_upcoming = income_this_month(bills) + load_one_time_items('income')
    total_month = sum(b[1] for b in all_bills_month if b[0] not in ignored) + sum(b[1] for b in ot_bills_month if b[0] not in ignored)

    # Payment evidence from the DB
    txns = load_month_transactions(month_start, window)
    manual_paid = get_manual_paid_names(month_str)

    # Build combined chronological monthly snapshot
    # Status: True=paid, False=pending, None=ignored
    paycheck_net = get_paycheck_net_amount()
    combined = (
        [("bill", b[0], b[1], b[2], None if b[0] in ignored else (bill_is_paid(b[0], b[1], txns_near_due(txns, b[2]), b[3], b[4], b[5]) or b[0] in manual_paid))
         for b in all_bills_month]
        + [("income", i[0], i[1], i[2], None if i[0] in ignored else (bill_is_paid(i[0], i[1], txns_near_due(txns, i[2]), i[3], i[4], i[5]) or i[0] in manual_paid))
           for i in all_income_month]
        + [("bill", b[0], b[1], b[2], None if b[0] in ignored else b[0] in manual_paid)
           for b in ot_bills_month]
        + [("income", i[0], i[1], i[2], None if i[0] in ignored else i[0] in manual_paid)
           for i in ot_income_month]
        + [("paycheck", "Paycheck", paycheck_net or 0, pd, paycheck_is_received(pd, txns))
           for pd in paychecks_in_window(month_start, window)]
    )
    combined.sort(key=lambda x: (x[3], 0 if x[0] == "paycheck" else 1))

    def fmt_row(row):
        typ, name, amt, dt, paid = row
        ds = dt.strftime("%b %-d")
        mark = "✓ " if paid else ("~ " if paid is None else "  ")
        if typ == "paycheck":
            return f"{mark}\U0001f4b5 {name} +${amt:,.0f} — {ds}"
        if typ == "income":
            return f"{mark}{name} +${amt:,.0f} — {ds}"
        return f"{mark}{name} ${amt:,.0f} — {ds}"

    list_lines = "\n".join(fmt_row(r) for r in combined) or "  (none)"

    # Top summary sections
    payday_str = payday.strftime("%a %b %-d")
    income_lines = "\n".join(
        f"  {i[0]} +${i[1]:,.0f} — {i[2].strftime('%b %-d')}" for i in all_income_upcoming
    )
    income_section = f"\U0001f49a Expected income:\n{income_lines}\n" if income_lines else ""

    gas_mo, gas_pd = spending_suggestion("TRANSPORTATION_GAS")
    groc_mo, groc_pd = spending_suggestion("FOOD_AND_DRINK_GROCERIES")
    suggestion_lines = []
    if gas_mo is not None:
        suggestion_lines.append(f"  ⛽ Gas  avg ${gas_mo:,.0f}/mo → ~${gas_pd:,.0f} this period")
    if groc_mo is not None:
        suggestion_lines.append(f"  \U0001f6d2 Groceries  avg ${groc_mo:,.0f}/mo → ~${groc_pd:,.0f} this period")
    suggestion_section = (
        "\U0001f4ca Budget suggestions (next 2 weeks):\n" + "\n".join(suggestion_lines) + "\n"
        if suggestion_lines else ""
    )

    month_label = today.strftime("%B %Y")

    if safe_money < threshold:
        body = (
            f"⚠️ LOW FUNDS ⚠️\n"
            f"\U0001f4b0 Balance: ${balance:,.0f}\n"
            f"\U0001f4c5 Next paycheck: {payday_str} ({days_until} days)\n"
            f"\U0001f4b3 Due before next paycheck: ${total_due:,.0f}  |  Full month: ${total_month:,.0f}\n"
            f"{income_section}"
            f"\U0001f6a8 Safe money: ${safe_money:,.0f} — WATCH IT\n"
            f"{suggestion_section}"
            f"\n\U0001f4c6 {month_label}:\n{list_lines}"
        )
    else:
        body = (
            f"\U0001f4b0 Balance: ${balance:,.0f}\n"
            f"\U0001f4c5 Next paycheck: {payday_str} ({days_until} days)\n"
            f"\U0001f4b3 Due before next paycheck: ${total_due:,.0f}  |  Full month: ${total_month:,.0f}\n"
            f"{income_section}"
            f"✅ Safe money: ${safe_money:,.0f}\n"
            f"{suggestion_section}"
            f"\n\U0001f4c6 {month_label}:\n{list_lines}"
        )

    if today.day == 1 and today.month in (1, 5, 9):
        body += "\n\n\U0001f4cb Reminder: run detect_bills.py to refresh recurring bill detection."

    subject = "⚠️ Daily Balance Alert" if safe_money < threshold else "\U0001f4b0 Daily Balance Update"
    send_email(subject, body, config)
    logging.info(f"Email sent — payday={payday} days_until={days_until}")
    print(body)


if __name__ == "__main__":
    main()
