import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median, stdev
from dotenv import load_dotenv
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
import plaid_client as pc

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

with open(Path(__file__).parent / "config.json") as _f:
    _config = json.load(_f)

client = pc.make_client(_config)
access_token = pc.access_token(_config)
end_date = date.today()
start_date = end_date - timedelta(days=730)

print("Fetching transactions (this may take a moment)...")

all_transactions = []
offset = 0
while True:
    resp = client.transactions_get(
        TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(count=500, offset=offset),
        )
    )
    txns = resp["transactions"]
    all_transactions.extend(txns)
    offset += len(txns)
    if offset >= resp["total_transactions"]:
        break

print(f"Fetched {len(all_transactions)} transactions over 2 years.")

debits = [t for t in all_transactions if t["amount"] > 0]


def normalize(name):
    name = (name or "").lower()
    name = re.sub(r"[0-9]", "", name)
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def classify_price_window(amounts):
    """Return max allowed coefficient of variation based on bill type."""
    med = median(amounts)
    if med < 20:
        return 0.15   # small fixed subscriptions — tight window
    if med < 200:
        return 0.40   # utilities, gym, mid-range — moderate variance OK
    return 0.30       # large bills (mortgage, insurance) — some variance OK


def is_monthly(sorted_dates):
    """Check if dates occur roughly every 25–37 days on average."""
    if len(sorted_dates) < 3:
        return False
    intervals = [
        (sorted_dates[i + 1] - sorted_dates[i]).days
        for i in range(len(sorted_dates) - 1)
    ]
    med_interval = median(intervals)
    return 25 <= med_interval <= 37


merchant_txns = defaultdict(list)
for t in debits:
    key = normalize(t.get("merchant_name") or t.get("name", ""))
    if key:
        merchant_txns[key].append(t)

candidates = []
for merchant, txns in merchant_txns.items():
    if len(txns) < 3:
        continue

    sorted_dates = sorted(t["date"] for t in txns)
    if not is_monthly(sorted_dates):
        continue

    amounts = [t["amount"] for t in txns]
    med_amount = median(amounts)
    if med_amount < 2:
        continue

    if len(amounts) > 1:
        cv = stdev(amounts) / med_amount if med_amount > 0 else 1
        if cv > classify_price_window(amounts):
            continue

    days = [t["date"].day for t in txns]
    med_day = int(median(days))
    display_name = (txns[0].get("merchant_name") or txns[0].get("name", merchant)).strip()
    candidates.append({
        "name": display_name,
        "amount": round(med_amount, 2),
        "day_of_month": med_day,
        "enabled": True,
    })

candidates.sort(key=lambda x: x["day_of_month"])

bills = candidates

bills_path = Path(__file__).parent / "bills.json"
with open(bills_path, "w") as f:
    json.dump(bills, f, indent=2)

print(f"\nDetected {len(candidates)} recurring bills:\n")
print(f"{'Name':<40} {'Amount':>9}  {'Day':>4}")
print("-" * 58)
for b in bills:
    print(f"{b['name']:<40} ${b['amount']:>8.2f}  {b['day_of_month']:>4}")

print(f"\nSaved to bills.json — review before running main.py")
print("NOTE: Any bills you added manually will be overwritten. Review the output carefully.")
