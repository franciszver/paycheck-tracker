import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions
import plaid_client as pc

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "transactions.db"

with open(BASE_DIR / "config.json") as f:
    config = json.load(f)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id      TEXT PRIMARY KEY,
            account_id          TEXT NOT NULL,
            date                TEXT NOT NULL,
            authorized_date     TEXT,
            name                TEXT NOT NULL,
            merchant_name       TEXT,
            amount              REAL NOT NULL,
            category            TEXT,
            category_primary    TEXT,
            category_detailed   TEXT,
            payment_channel     TEXT,
            pending             INTEGER NOT NULL DEFAULT 0,
            iso_currency_code   TEXT DEFAULT 'USD',
            inserted_at         TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_txn_date        ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_txn_merchant    ON transactions(merchant_name);
        CREATE INDEX IF NOT EXISTS idx_txn_category    ON transactions(category_primary);
        CREATE INDEX IF NOT EXISTS idx_txn_amount      ON transactions(amount);
        CREATE INDEX IF NOT EXISTS idx_txn_pending     ON transactions(pending);

        CREATE TABLE IF NOT EXISTS accounts (
            account_id          TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            official_name       TEXT,
            type                TEXT,
            subtype             TEXT,
            balance_available   REAL,
            balance_current     REAL,
            iso_currency_code   TEXT DEFAULT 'USD',
            last_synced         TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manual_payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_name  TEXT NOT NULL,
            month      TEXT NOT NULL,
            marked_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(bill_name, month)
        );

        CREATE TABLE IF NOT EXISTS one_time_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            month         TEXT NOT NULL,
            name          TEXT NOT NULL,
            amount        REAL NOT NULL,
            day_of_month  INTEGER NOT NULL,
            type          TEXT NOT NULL DEFAULT 'bill',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(month, name)
        );

        CREATE TABLE IF NOT EXISTS date_overrides (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_name     TEXT NOT NULL,
            month         TEXT NOT NULL,
            new_day       INTEGER NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(bill_name, month)
        );

        CREATE TABLE IF NOT EXISTS ignored_bills (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_name     TEXT NOT NULL,
            month         TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(bill_name, month)
        );

        CREATE TABLE IF NOT EXISTS amount_overrides (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_name     TEXT NOT NULL,
            month         TEXT NOT NULL,
            new_amount    REAL NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(bill_name, month)
        );
    """)
    conn.commit()


def sync_accounts(conn, client, token):
    resp = client.accounts_balance_get(
        AccountsBalanceGetRequest(
            access_token=token,
            options=AccountsBalanceGetRequestOptions(
                min_last_updated_datetime=datetime.now(timezone.utc),
            ),
        )
    )
    now = datetime.utcnow().isoformat()
    for a in resp["accounts"]:
        b = a["balances"]
        conn.execute("""
            INSERT OR REPLACE INTO accounts
              (account_id, name, official_name, type, subtype,
               balance_available, balance_current, iso_currency_code, last_synced)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            a["account_id"],
            a["name"],
            a.get("official_name"),
            str(a["type"]),
            str(a["subtype"]),
            b["available"],
            b["current"],
            b.get("iso_currency_code", "USD"),
            now,
        ))
    conn.commit()


def upsert_transaction(conn, t):
    cat = t.get("category")
    pfc = t.get("personal_finance_category") or {}
    conn.execute("""
        INSERT OR REPLACE INTO transactions
          (transaction_id, account_id, date, authorized_date, name, merchant_name,
           amount, category, category_primary, category_detailed,
           payment_channel, pending, iso_currency_code)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        t["transaction_id"],
        t["account_id"],
        str(t["date"]),
        str(t["authorized_date"]) if t.get("authorized_date") else None,
        t["name"],
        t.get("merchant_name"),
        t["amount"],
        json.dumps(cat) if cat else None,
        pfc.get("primary"),
        pfc.get("detailed"),
        t.get("payment_channel"),
        1 if t.get("pending") else 0,
        t.get("iso_currency_code", "USD"),
    ))


def sync_transactions(conn, client, token, full_reset=False):
    if full_reset:
        conn.execute("DELETE FROM sync_state WHERE key='cursor'")
        conn.commit()
        print("Cursor reset — doing full sync.")

    row = conn.execute("SELECT value FROM sync_state WHERE key='cursor'").fetchone()
    cursor = row["value"] if row else None

    added = modified = removed = 0
    has_more = True

    while has_more:
        kwargs = {"access_token": token}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))

        for t in resp["added"]:
            upsert_transaction(conn, t)
            added += 1
        for t in resp["modified"]:
            upsert_transaction(conn, t)
            modified += 1
        for t in resp["removed"]:
            conn.execute("DELETE FROM transactions WHERE transaction_id=?",
                         (t["transaction_id"],))
            removed += 1

        cursor = resp["next_cursor"]
        has_more = resp["has_more"]
        conn.commit()

    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('cursor', ?)", (cursor,))
    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync', ?)",
                 (datetime.utcnow().isoformat(),))
    conn.commit()
    return added, modified, removed


def main():
    full_reset = "--full-reset" in sys.argv
    client = pc.make_client(config)
    token = pc.access_token(config)

    conn = get_db()
    init_db(conn)
    sync_accounts(conn, client, token)
    added, modified, removed = sync_transactions(conn, client, token, full_reset=full_reset)
    conn.close()

    print(f"Sync complete — added: {added}, modified: {modified}, removed: {removed}")


if __name__ == "__main__":
    main()
