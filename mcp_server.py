import json
import re
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "transactions.db"

mcp = FastMCP("paycheck-tracker")

CATEGORY_ALIASES = {
    "restaurants": "FOOD_AND_DRINK",
    "restaurant": "FOOD_AND_DRINK",
    "food": "FOOD_AND_DRINK",
    "dining": "FOOD_AND_DRINK",
    "gas": "TRANSPORTATION",
    "fuel": "TRANSPORTATION",
    "transport": "TRANSPORTATION",
    "groceries": "FOOD_AND_DRINK",
    "grocery": "FOOD_AND_DRINK",
    "shopping": "GENERAL_MERCHANDISE",
    "entertainment": "ENTERTAINMENT",
    "utilities": "UTILITIES",
    "medical": "MEDICAL",
    "health": "MEDICAL",
    "travel": "TRAVEL",
    "income": "INCOME",
    "transfer": "TRANSFER_IN",
}


def get_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def no_db():
    return {"error": "No transaction data. Run `python3 sync_transactions.py` first or use sync_now."}


def resolve_category(category: str) -> str:
    return CATEGORY_ALIASES.get(category.lower(), category.upper())


def parse_date(s: str) -> str:
    if not s:
        return None
    s = s.strip().lower()
    today = date.today()
    if s == "today":
        return str(today)
    if s == "yesterday":
        return str(today - timedelta(days=1))
    if s == "this month":
        return str(date(today.year, today.month, 1))
    if s == "last month":
        first = date(today.year, today.month, 1) - timedelta(days=1)
        return str(date(first.year, first.month, 1))
    return s


@mcp.tool()
def get_balance() -> dict:
    """Get current bank account balances."""
    conn = get_db()
    if not conn:
        return no_db()
    rows = conn.execute(
        "SELECT name, subtype, balance_available, balance_current, last_synced FROM accounts"
    ).fetchall()
    conn.close()
    if not rows:
        return {"error": "No account data. Run sync_now first."}
    return [dict(r) for r in rows]


@mcp.tool()
def search_transactions(query: str, days: int = 30, limit: int = 50) -> list:
    """Search transactions by merchant name or description keyword."""
    conn = get_db()
    if not conn:
        return [no_db()]
    since = str(date.today() - timedelta(days=days))
    rows = conn.execute("""
        SELECT date, merchant_name, name, amount, category_primary, pending
        FROM transactions
        WHERE date >= ? AND pending = 0
          AND (LOWER(name) LIKE ? OR LOWER(COALESCE(merchant_name,'')) LIKE ?)
        ORDER BY date DESC LIMIT ?
    """, (since, f"%{query.lower()}%", f"%{query.lower()}%", limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def get_transactions(
    start_date: str = None,
    end_date: str = None,
    min_amount: float = None,
    max_amount: float = None,
    merchant: str = None,
    limit: int = 100,
) -> list:
    """Get transactions filtered by date range, amount range, or merchant."""
    conn = get_db()
    if not conn:
        return [no_db()]

    clauses = ["pending = 0"]
    params = []

    start = parse_date(start_date) or str(date.today() - timedelta(days=30))
    clauses.append("date >= ?")
    params.append(start)

    if end_date:
        clauses.append("date <= ?")
        params.append(parse_date(end_date))
    if min_amount is not None:
        clauses.append("amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append("amount <= ?")
        params.append(max_amount)
    if merchant:
        clauses.append("(LOWER(COALESCE(merchant_name,'')) LIKE ? OR LOWER(name) LIKE ?)")
        params += [f"%{merchant.lower()}%", f"%{merchant.lower()}%"]

    params.append(limit)
    rows = conn.execute(
        f"SELECT date, merchant_name, name, amount, category_primary FROM transactions "
        f"WHERE {' AND '.join(clauses)} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def spending_by_category(days: int = 30, category: str = None) -> dict:
    """Summarize spending by category. Pass category to drill into a specific one."""
    conn = get_db()
    if not conn:
        return no_db()
    since = str(date.today() - timedelta(days=days))

    if category:
        cat_key = resolve_category(category)
        rows = conn.execute("""
            SELECT date, merchant_name, name, amount
            FROM transactions
            WHERE date >= ? AND amount > 0 AND pending = 0
              AND UPPER(COALESCE(category_primary,'')) LIKE ?
            ORDER BY amount DESC LIMIT 50
        """, (since, f"%{cat_key}%")).fetchall()
        conn.close()
        items = [dict(r) for r in rows]
        return {"category": cat_key, "total": round(sum(r["amount"] for r in items), 2), "transactions": items}

    rows = conn.execute("""
        SELECT COALESCE(category_primary, 'UNCATEGORIZED') as category,
               COUNT(*) as count,
               ROUND(SUM(amount), 2) as total
        FROM transactions
        WHERE date >= ? AND amount > 0 AND pending = 0
        GROUP BY category ORDER BY total DESC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def get_spending_trend(category: str, periods: int = 3, period_type: str = "month") -> list:
    """Compare spending in a category across recent months or weeks."""
    conn = get_db()
    if not conn:
        return [no_db()]
    cat_key = resolve_category(category)

    if period_type == "month":
        fmt = "%Y-%m"
        days_back = periods * 31
    else:
        fmt = "%Y-W%W"
        days_back = periods * 7

    since = str(date.today() - timedelta(days=days_back))
    rows = conn.execute(f"""
        SELECT strftime('{fmt}', date) as period,
               COUNT(*) as count,
               ROUND(SUM(amount), 2) as total
        FROM transactions
        WHERE date >= ? AND amount > 0 AND pending = 0
          AND UPPER(COALESCE(category_primary,'')) LIKE ?
        GROUP BY period ORDER BY period
    """, (since, f"%{cat_key}%")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@mcp.tool()
def get_income(days: int = 60) -> list:
    """Get deposits and income transactions (negative amounts = money in)."""
    conn = get_db()
    if not conn:
        return [no_db()]
    since = str(date.today() - timedelta(days=days))
    rows = conn.execute("""
        SELECT date, merchant_name, name, amount
        FROM transactions
        WHERE date >= ? AND amount < 0 AND pending = 0
        ORDER BY date DESC LIMIT 50
    """, (since,)).fetchall()
    conn.close()
    return [{"date": r["date"], "name": r["name"], "merchant": r["merchant_name"],
             "amount": abs(r["amount"])} for r in rows]


@mcp.tool()
def get_bills_status(month: str = None) -> dict:
    """Show bills from bills.json and whether matching transactions exist this month."""
    conn = get_db()
    if not conn:
        return no_db()

    today = date.today()
    if month:
        year, mo = int(month[:4]), int(month[5:7])
    else:
        year, mo = today.year, today.month

    month_start = str(date(year, mo, 1))
    next_mo = mo + 1 if mo < 12 else 1
    next_yr = year if mo < 12 else year + 1
    month_end = str(date(next_yr, next_mo, 1) - timedelta(days=1))

    with open(BASE_DIR / "bills.json") as f:
        bills = json.load(f)

    result = []
    for b in bills:
        if not b.get("enabled", True):
            continue
        name = b["name"]
        amount = b["amount"]
        bill_type = b.get("type", "bill")
        keyword = re.sub(r"\s+\(.*\)", "", name).split()[0].lower()
        match = conn.execute("""
            SELECT date, amount FROM transactions
            WHERE date BETWEEN ? AND ?
              AND LOWER(COALESCE(merchant_name, name)) LIKE ?
            ORDER BY date LIMIT 1
        """, (month_start, month_end, f"%{keyword}%")).fetchone()
        result.append({
            "name": name,
            "expected": amount,
            "type": bill_type,
            "status": "paid" if match else "pending",
            "paid_date": match["date"] if match else None,
            "paid_amount": round(abs(match["amount"]), 2) if match else None,
        })

    conn.close()
    return result


@mcp.tool()
def sync_now() -> dict:
    """Pull the latest transactions from Plaid right now."""
    result = subprocess.run(
        ["python3", str(BASE_DIR / "sync_transactions.py")],
        capture_output=True, text=True, timeout=60,
    )
    output = result.stdout.strip() or result.stderr.strip()
    return {"status": "ok" if result.returncode == 0 else "error", "output": output}


if __name__ == "__main__":
    mcp.run()
