#!/usr/bin/env python3
import getpass
from pathlib import Path
from dotenv import set_key

SECRETS_FILE = Path.home() / ".config" / "paycheck-tracker" / "secrets.env"

print("Leave blank to keep existing value.\n")

sandbox_secret = getpass.getpass("Plaid sandbox secret: ")
if sandbox_secret:
    set_key(str(SECRETS_FILE), "PLAID_SECRET_SANDBOX", sandbox_secret)
    print("  ✓ PLAID_SECRET_SANDBOX updated")

prod_secret = getpass.getpass("Plaid production secret: ")
if prod_secret:
    set_key(str(SECRETS_FILE), "PLAID_SECRET_PRODUCTION", prod_secret)
    print("  ✓ PLAID_SECRET_PRODUCTION updated")

sandbox_token = input("Plaid sandbox access token: ").strip()
if sandbox_token:
    set_key(str(SECRETS_FILE), "PLAID_ACCESS_TOKEN_SANDBOX", sandbox_token)
    print("  ✓ PLAID_ACCESS_TOKEN_SANDBOX updated")

prod_token = input("Plaid production access token: ").strip()
if prod_token:
    set_key(str(SECRETS_FILE), "PLAID_ACCESS_TOKEN_PRODUCTION", prod_token)
    print("  ✓ PLAID_ACCESS_TOKEN_PRODUCTION updated")

password = getpass.getpass("Gmail app password: ")
if password:
    set_key(str(SECRETS_FILE), "GMAIL_APP_PASSWORD", password)
    print("  ✓ GMAIL_APP_PASSWORD updated")

discord_token = getpass.getpass("Discord bot token: ")
if discord_token:
    set_key(str(SECRETS_FILE), "DISCORD_BOT_TOKEN", discord_token)
    print("  ✓ DISCORD_BOT_TOKEN updated")

discord_guild = input("Discord guild (server) ID: ").strip()
if discord_guild:
    set_key(str(SECRETS_FILE), "DISCORD_GUILD_ID", discord_guild)
    print("  ✓ DISCORD_GUILD_ID updated")

print("\nDone.")
