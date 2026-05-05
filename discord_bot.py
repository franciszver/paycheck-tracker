import asyncio
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
import discord
from discord import app_commands

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"
load_dotenv(SECRETS_FILE)

BASE_DIR = Path(__file__).parent
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
    "Amounts, dates, merchant names, and spending categories are fine to include."
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

        chunks = [answer[i:i+2000] for i in range(0, max(len(answer), 1), 2000)]
        for chunk in chunks:
            await interaction.followup.send(chunk)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
