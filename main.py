import json
import os
import re
import smtplib
import sqlite3
import logging
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv
import holidays
from plaid.model.accounts_get_request import AccountsGetRequest
import plaid_client as pc

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


def payday_anchor():
    return date.fromisoformat(config["paycheck"]["last_friday"])


def nth_payday_of_month(year, month, n):
    """Return the nth payday (1-indexed) that falls in the given month, or None."""
    anchor = payday_anchor()
    us_holidays = holidays.UnitedStates(years=[year])
    # walk anchor forward/backward to find paydays in the target month
    start = date(year, month, 1)
    # align anchor to nearest payday on or before start
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


def resolve_due_date(b, today):
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


def bills_due_this_month(bill_list, start=None):
    """All bills (not income) through end of month. start defaults to today."""
    today = date.today()
    ref = start if start is not None else today
    window = end_of_month(today)
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or _is_income(b):
            continue
        due_date = resolve_due_date(b, ref)
        if due_date and ref <= due_date <= window:
            due.append((b["name"], b["amount"], due_date))
    due.sort(key=lambda x: x[2])
    return due


def income_this_month(bill_list, start=None):
    """Income entries through end of month. start defaults to today."""
    today = date.today()
    ref = start if start is not None else today
    window = end_of_month(today)
    due = []
    for b in bill_list:
        if not b.get("enabled", True) or not _is_income(b):
            continue
        due_date = resolve_due_date(b, ref)
        if due_date and ref <= due_date <= window:
            due.append((b["name"], b["amount"], due_date))
    due.sort(key=lambda x: x[2])
    return due


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
            due.append((b["name"], b["amount"], due_date))
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
            due.append((b["name"], b["amount"], due_date))
    due.sort(key=lambda x: x[2])
    return due


def load_month_transactions(month_start, month_end):
    """Fetch settled checking-account transactions for the month for payment-evidence checks."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT t.name, t.merchant_name, t.category_detailed, t.date, t.amount"
        " FROM transactions t"
        " JOIN accounts a ON a.account_id = t.account_id"
        " WHERE a.subtype = 'checking'"
        "   AND t.date BETWEEN ? AND ? AND t.pending=0",
        (str(month_start), str(month_end)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bill_is_paid(name, amount, txns):
    """True if a checking transaction matches by (keyword + amount ±10%) or full name."""
    keyword = re.sub(r"\s+\(.*\)", "", name).split()[0].lower()
    full = name.lower()
    tolerance = amount * 0.10
    return any(
        (keyword in (t["name"] or "").lower() or keyword in (t["merchant_name"] or "").lower())
        and abs(abs(t["amount"]) - amount) <= tolerance
        or full in (t["name"] or "").lower()
        or full in (t["merchant_name"] or "").lower()
        for t in txns
    )


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
    resp = client.accounts_get(
        AccountsGetRequest(access_token=pc.access_token(config))
    )
    account = next(a for a in resp["accounts"] if str(a["subtype"]) == "checking")
    balance = account["balances"]["available"] or account["balances"]["current"]

    today = date.today()
    month_start = date(today.year, today.month, 1)
    window = end_of_month(today)

    payday, days_until = next_payday(config["paycheck"]["last_friday"])
    payday_bills = bills_due_before_payday(payday, bills)
    payday_income = income_before_payday(payday, bills)
    total_due = sum(b[1] for b in payday_bills)
    total_income_safe = sum(i[1] for i in payday_income)
    safe_money = balance - total_due + total_income_safe
    threshold = config.get("low_balance_threshold", 200)

    # Full-month data for the snapshot list
    all_bills_month = bills_due_this_month(bills, start=month_start)
    all_income_month = income_this_month(bills, start=month_start)
    all_income_upcoming = income_this_month(bills)   # today-cutoff for top summary
    total_month = sum(b[1] for b in all_bills_month)

    # Payment evidence from the DB
    txns = load_month_transactions(month_start, window)

    # Build combined chronological monthly snapshot
    paycheck_net = get_paycheck_net_amount()
    combined = (
        [("bill", b[0], b[1], b[2], bill_is_paid(b[0], b[1], txns)) for b in all_bills_month]
        + [("income", i[0], i[1], i[2], bill_is_paid(i[0], i[1], txns)) for i in all_income_month]
        + [("paycheck", "Paycheck", paycheck_net or 0, pd, paycheck_is_received(pd, txns))
           for pd in paychecks_in_window(month_start, window)]
    )
    combined.sort(key=lambda x: (x[3], 0 if x[0] == "paycheck" else 1))

    def fmt_row(row):
        typ, name, amt, dt, paid = row
        ds = dt.strftime("%b %-d")
        mark = "✓ " if paid else "  "
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
