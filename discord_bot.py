import asyncio
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import discord
from discord import app_commands
from bill_utils import (
    bills_due_this_month, income_this_month, load_one_time_items,
    invalidate_amount_override_cache, invalidate_date_override_cache,
)
from sync_transactions import init_db

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "transactions.db"
BILLS_FILE = BASE_DIR / "bills.json"

with open(BASE_DIR / "config.json") as f:
    config = json.load(f)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
ALLOWED_USER_IDS = set(config["discord"]["allowed_user_ids"])
KNOWN_ACCOUNTS = config["discord"].get("known_account_suffixes", [])
ACCOUNT_HOLDER_NAME = config["discord"].get("account_holder_name", "")
CLAUDE_PATH = config.get("claude_path", "claude")

SYSTEM_PROMPT = (
    "You are a personal finance assistant. Answer concisely and clearly. "
    "Never include bank account numbers, routing numbers, or account identifiers in your response. "
    "Reference accounts as 'your checking account' or 'your savings account' only — never by number. "
    "Never include the user's full name or partial name as it appears in transaction records. "
    "Amounts, dates, merchant names, and spending categories are fine to include. "
    "Bills and payments (what's been paid) come from get_bills_status; income and deposits "
    "(e.g. gifts, transfers from family, paychecks) come from get_income and are not 'payments.'"
)


def _build_name_patterns(name: str) -> list:
    """Build regex patterns to redact the account holder's name from Claude responses."""
    if not name:
        return []
    parts = name.upper().split()
    if len(parts) < 2:
        return [re.escape(name)]
    first, last = parts[0], parts[-1]
    return [
        rf"{re.escape(first)}\s+(?:\S+\s+)?{re.escape(last)}",
        rf"{re.escape(last)}[,\s]+{re.escape(first)}",
    ]


NAME_PATTERNS = _build_name_patterns(ACCOUNT_HOLDER_NAME)
ACCOUNT_PATTERNS = [
    r"(?:CHK|SAV|ACCT|ACCOUNT|ending|mask)[:\s#]*\d{4,}",
    r"\b\d{4}\b(?=\s*(?:checking|savings|account))",
]


def sanitize(text: str) -> str:
    for acct in KNOWN_ACCOUNTS:
        text = text.replace(acct, "****")
    for pattern in NAME_PATTERNS:
        text = re.sub(pattern, "****", text, flags=re.IGNORECASE)
    for pattern in ACCOUNT_PATTERNS:
        text = re.sub(pattern, "****", text, flags=re.IGNORECASE)
    return text


class FinanceBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Slash commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


# Ensure the bill-override tables exist even if the bot starts before the
# first `sync_transactions.py` run — that script is the sole schema owner,
# this just guards against running the bot out of order.
_conn = sqlite3.connect(DB_PATH)
init_db(_conn)
_conn.close()

bot = FinanceBot()


@bot.tree.command(name="askfinance", description="Ask Claude about your finances")
@app_commands.describe(question="What do you want to know about your finances?")
async def askfinance(interaction: discord.Interaction, question: str):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    await interaction.response.defer()

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_PATH, "-p", question,
            "--append-system-prompt", SYSTEM_PROMPT,
            "--allowedTools", "mcp__paycheck-tracker__*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await interaction.followup.send("⏱️ Query timed out. Try a simpler question.")
            return

        raw = stdout.decode().strip()
        if not raw:
            raw = stderr.decode().strip() or "No response received."

        answer = sanitize(raw)

        # Split into 2000-char chunks for Discord's limit
        chunks = [answer[i:i+2000] for i in range(0, max(len(answer), 1), 2000)]
        for chunk in chunks:
            await interaction.followup.send(chunk)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


def _next_month_start() -> date:
    today = date.today()
    nm = today.month + 1 if today.month < 12 else 1
    ny = today.year if today.month < 12 else today.year + 1
    return date(ny, nm, 1)


def _month_key(month_start: date) -> str:
    return month_start.strftime("%Y-%m")


def _resolve_month_start(month) -> date:
    if month is not None and month.value == "next":
        return _next_month_start()
    today = date.today()
    return date(today.year, today.month, 1)


def _bill_names_for_month(current: str, month_start: date) -> list[str]:
    with open(BILLS_FILE) as f:
        bill_list = json.load(f)
    names = (
        [b[0] for b in bills_due_this_month(bill_list, start=month_start)]
        + [i[0] for i in income_this_month(bill_list, start=month_start)]
        + [b[0] for b in load_one_time_items(start=month_start)]
    )
    return [n for n in names if current.lower() in n.lower()]


def _current_month_one_time_names(current: str) -> list[str]:
    month = date.today().strftime("%Y-%m")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name FROM one_time_items WHERE month=?", (month,)
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    names = [r["name"] for r in rows]
    return [n for n in names if current.lower() in n.lower()]


@bot.tree.command(name="markpaid", description="Manually mark a bill as paid")
@app_commands.describe(bill="Bill to mark as paid", month="Which month to mark paid")
@app_commands.choices(month=[
    app_commands.Choice(name="This month", value="this"),
    app_commands.Choice(name="Next month", value="next"),
])
async def markpaid(
    interaction: discord.Interaction,
    bill: str,
    month: app_commands.Choice[str] = None,
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    month_start = _resolve_month_start(month)
    month_str = _month_key(month_start)
    month_label = month_start.strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO manual_payments (bill_name, month) VALUES (?, ?)",
        (bill, month_str),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Marked **{bill}** as paid for {month_label}.")


async def _bill_month_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    use_next = getattr(interaction.namespace, "month", None) == "next"
    month_start = _next_month_start() if use_next else date(date.today().year, date.today().month, 1)
    names = _bill_names_for_month(current, month_start)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


markpaid.autocomplete("bill")(_bill_month_autocomplete)


@bot.tree.command(name="unmarkpaid", description="Remove a manual paid mark")
@app_commands.describe(bill="Bill to unmark", month="Which month to unmark")
@app_commands.choices(month=[
    app_commands.Choice(name="This month", value="this"),
    app_commands.Choice(name="Next month", value="next"),
])
async def unmarkpaid(
    interaction: discord.Interaction,
    bill: str,
    month: app_commands.Choice[str] = None,
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    month_start = _resolve_month_start(month)
    month_str = _month_key(month_start)
    month_label = month_start.strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "DELETE FROM manual_payments WHERE bill_name=? AND month=?",
        (bill, month_str),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount:
        await interaction.response.send_message(
            f"🗑️ Removed manual paid mark for **{bill}** ({month_label})."
        )
    else:
        await interaction.response.send_message(
            f"⚠️ No manual mark found for **{bill}** in {month_label}.", ephemeral=True
        )


unmarkpaid.autocomplete("bill")(_bill_month_autocomplete)


@bot.tree.command(name="additem", description="Add a one-time expense or income for this month")
@app_commands.describe(
    name="Name of the item",
    amount="Dollar amount",
    day="Day of month it's due (1–31)",
    item_type="Expense (reduces safe money) or Income (offsets bills)",
)
@app_commands.choices(item_type=[
    app_commands.Choice(name="Expense", value="bill"),
    app_commands.Choice(name="Income", value="income"),
])
async def additem(
    interaction: discord.Interaction,
    name: str,
    amount: float,
    day: int,
    item_type: app_commands.Choice[str],
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    if not 1 <= day <= 31:
        await interaction.response.send_message("❌ Day must be between 1 and 31.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return

    month = date.today().strftime("%Y-%m")
    month_label = date.today().strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO one_time_items (month, name, amount, day_of_month, type) VALUES (?,?,?,?,?)",
            (month, name, amount, day, item_type.value),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        await interaction.response.send_message(
            f"⚠️ **{name}** already exists for {month_label}. Remove it first with `/removeitem`.",
            ephemeral=True,
        )
        return
    conn.close()

    kind = "income" if item_type.value == "income" else "expense"
    await interaction.response.send_message(
        f"✅ Added one-time {kind} **{name}** — ${amount:,.2f} due day {day} ({month_label})."
    )


@bot.tree.command(name="removeitem", description="Remove a one-time item from this month")
@app_commands.describe(name="Item to remove")
async def removeitem(interaction: discord.Interaction, name: str):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    month = date.today().strftime("%Y-%m")
    month_label = date.today().strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "DELETE FROM one_time_items WHERE month=? AND name=?",
        (month, name),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount:
        await interaction.response.send_message(
            f"🗑️ Removed **{name}** from {month_label}."
        )
    else:
        await interaction.response.send_message(
            f"⚠️ No one-time item **{name}** found for {month_label}.", ephemeral=True
        )


@removeitem.autocomplete("name")
async def removeitem_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    names = _current_month_one_time_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@bot.tree.command(name="ignorebill", description="Ignore a bill for this month (not paid, not counted)")
@app_commands.describe(bill="Bill to ignore", month="Which month")
@app_commands.choices(month=[
    app_commands.Choice(name="This month", value="this"),
    app_commands.Choice(name="Next month", value="next"),
])
async def ignorebill(
    interaction: discord.Interaction,
    bill: str,
    month: app_commands.Choice[str] = None,
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    month_start = _resolve_month_start(month)
    month_str = _month_key(month_start)
    month_label = month_start.strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO ignored_bills (bill_name, month) VALUES (?, ?)",
        (bill, month_str),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"Ignored **{bill}** for {month_label} — won't count toward safe money.")


ignorebill.autocomplete("bill")(_bill_month_autocomplete)


@bot.tree.command(name="unignorebill", description="Remove an ignore mark from a bill")
@app_commands.describe(bill="Bill to un-ignore", month="Which month")
@app_commands.choices(month=[
    app_commands.Choice(name="This month", value="this"),
    app_commands.Choice(name="Next month", value="next"),
])
async def unignorebill(
    interaction: discord.Interaction,
    bill: str,
    month: app_commands.Choice[str] = None,
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    month_start = _resolve_month_start(month)
    month_str = _month_key(month_start)
    month_label = month_start.strftime("%B %Y")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "DELETE FROM ignored_bills WHERE bill_name=? AND month=?",
        (bill, month_str),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount:
        await interaction.response.send_message(f"Removed ignore mark for **{bill}** ({month_label}).")
    else:
        await interaction.response.send_message(f"No ignore mark found for **{bill}** in {month_label}.", ephemeral=True)


unignorebill.autocomplete("bill")(_bill_month_autocomplete)


def _all_bill_names(current: str) -> list[str]:
    with open(BILLS_FILE) as f:
        bill_list = json.load(f)
    names = [b["name"] for b in bill_list if b.get("enabled", True)]
    return [n for n in names if current.lower() in n.lower()]


async def _all_bills_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    names = _all_bill_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@bot.tree.command(name="movedate", description="Move a bill's due date to a different day")
@app_commands.describe(
    bill="Bill or income item to move",
    day="New day of the month (1–31)",
    permanent="Permanent change or just this month?",
)
@app_commands.choices(permanent=[
    app_commands.Choice(name="This month only", value="no"),
    app_commands.Choice(name="Permanent", value="yes"),
])
async def movedate(
    interaction: discord.Interaction,
    bill: str,
    day: int,
    permanent: app_commands.Choice[str],
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not 1 <= day <= 31:
        await interaction.response.send_message("Day must be between 1 and 31.", ephemeral=True)
        return

    month_key = "permanent" if permanent.value == "yes" else date.today().strftime("%Y-%m")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO date_overrides (bill_name, month, new_day) VALUES (?, ?, ?)",
        (bill, month_key, day),
    )
    conn.commit()
    conn.close()
    invalidate_date_override_cache()

    if permanent.value == "yes":
        label = "permanently"
    else:
        label = f"for {date.today().strftime('%B %Y')}"
    await interaction.response.send_message(
        f"Moved **{bill}** to day **{day}** {label}."
    )


movedate.autocomplete("bill")(_all_bills_autocomplete)


@bot.tree.command(name="resetdate", description="Remove a date override for a bill")
@app_commands.describe(
    bill="Bill to reset",
    scope="Remove this month's override, the permanent override, or both?",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="This month only", value="month"),
    app_commands.Choice(name="Permanent", value="permanent"),
    app_commands.Choice(name="Both", value="both"),
])
async def resetdate(
    interaction: discord.Interaction,
    bill: str,
    scope: app_commands.Choice[str],
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    conn = sqlite3.connect(DB_PATH)
    month = date.today().strftime("%Y-%m")
    removed = 0
    if scope.value in ("month", "both"):
        c = conn.execute(
            "DELETE FROM date_overrides WHERE bill_name=? AND month=?",
            (bill, month),
        )
        removed += c.rowcount
    if scope.value in ("permanent", "both"):
        c = conn.execute(
            "DELETE FROM date_overrides WHERE bill_name=? AND month='permanent'",
            (bill,),
        )
        removed += c.rowcount
    conn.commit()
    conn.close()
    invalidate_date_override_cache()

    if removed:
        await interaction.response.send_message(
            f"Reset date override for **{bill}** ({scope.name})."
        )
    else:
        await interaction.response.send_message(
            f"No date override found for **{bill}** ({scope.name}).", ephemeral=True
        )


resetdate.autocomplete("bill")(_all_bills_autocomplete)


def _month_key_for_scope(scope: str) -> str:
    if scope == "permanent":
        return "permanent"
    return _month_key(_next_month_start() if scope == "next" else date.today())


@bot.tree.command(name="overrideamount", description="Override a bill's amount for a specific month or permanently")
@app_commands.describe(
    bill="Bill to override",
    amount="New amount in dollars",
    scope="Apply to this month, next month, or permanently?",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="This month only", value="this"),
    app_commands.Choice(name="Next month only", value="next"),
    app_commands.Choice(name="Permanent", value="permanent"),
])
async def overrideamount(
    interaction: discord.Interaction,
    bill: str,
    amount: float,
    scope: app_commands.Choice[str],
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if bill not in _all_bill_names(""):
        await interaction.response.send_message(f"Unknown bill: **{bill}**.", ephemeral=True)
        return
    if amount < 0:
        await interaction.response.send_message("Amount must be non-negative.", ephemeral=True)
        return

    month_key = _month_key_for_scope(scope.value)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO amount_overrides (bill_name, month, new_amount) VALUES (?, ?, ?)",
        (bill, month_key, amount),
    )
    conn.commit()
    conn.close()
    invalidate_amount_override_cache()

    if scope.value == "permanent":
        label = "permanently"
    else:
        year, mo = int(month_key[:4]), int(month_key[5:7])
        label = f"for {date(year, mo, 1).strftime('%B %Y')}"
    await interaction.response.send_message(f"Set **{bill}** amount to **${amount:,.2f}** {label}.")


overrideamount.autocomplete("bill")(_all_bills_autocomplete)


@bot.tree.command(name="resetamount", description="Remove an amount override for a bill")
@app_commands.describe(
    bill="Bill to reset",
    scope="Remove this month's override, next month's, the permanent override, or all?",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="This month", value="this"),
    app_commands.Choice(name="Next month", value="next"),
    app_commands.Choice(name="Permanent", value="permanent"),
    app_commands.Choice(name="All", value="all"),
])
async def resetamount(
    interaction: discord.Interaction,
    bill: str,
    scope: app_commands.Choice[str],
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if bill not in _all_bill_names(""):
        await interaction.response.send_message(f"Unknown bill: **{bill}**.", ephemeral=True)
        return

    conn = sqlite3.connect(DB_PATH)
    removed = 0
    if scope.value == "all":
        c = conn.execute("DELETE FROM amount_overrides WHERE bill_name=?", (bill,))
        removed = c.rowcount
    else:
        month_key = _month_key_for_scope(scope.value)
        c = conn.execute(
            "DELETE FROM amount_overrides WHERE bill_name=? AND month=?", (bill, month_key)
        )
        removed = c.rowcount
    conn.commit()
    conn.close()
    invalidate_amount_override_cache()

    if removed:
        await interaction.response.send_message(f"Removed amount override for **{bill}** ({scope.name}).")
    else:
        await interaction.response.send_message(f"No override found for **{bill}** ({scope.name}).")


resetamount.autocomplete("bill")(_all_bills_autocomplete)


@bot.tree.command(
    name="recalculate",
    description="Sync latest data, recalculate, and resend the daily balance email",
)
async def recalculate(interaction: discord.Interaction):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        # 1. Pull latest Plaid transactions into the DB
        sync = await asyncio.create_subprocess_exec(
            "python3", str(BASE_DIR / "sync_transactions.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, sync_err = await asyncio.wait_for(sync.communicate(), timeout=120)
        if sync.returncode != 0:
            await interaction.followup.send(
                f"❌ Sync failed: {sync_err.decode().strip() or 'unknown error'}"
            )
            return

        # 2. Recalculate + resend the email
        mail = await asyncio.create_subprocess_exec(
            "python3", str(BASE_DIR / "main.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, mail_err = await asyncio.wait_for(mail.communicate(), timeout=120)
        if mail.returncode != 0:
            await interaction.followup.send(
                f"❌ Email failed: {mail_err.decode().strip() or 'unknown error'}"
            )
            return

        await interaction.followup.send(
            "✅ Recalculated and resent the daily balance email."
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("⏱️ Timed out. Try again.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
