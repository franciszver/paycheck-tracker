# Paycheck Tracker

A personal finance system that sends you a daily email with your bank balance, upcoming bills, and "safe money" before your next paycheck — plus a Discord bot powered by Claude that can answer questions about your spending and transactions.

---

## What it does

- **Daily email at 8 AM** — shows your checking balance, next payday countdown, bills due before payday, and how much you can safely spend
- **Discord bot** — type `/askfinance how much did I spend on food last month?` and Claude reads your real transaction data to answer
- **Transaction database** — syncs your bank transactions locally via Plaid so Claude can query them privately on your machine

---

## How it all works

```
Plaid API  ──►  sync_transactions.py  ──►  transactions.db (SQLite)
                                                    │
                                        mcp_server.py (MCP tools)
                                                    │
Bank balance ──►  main.py  ──►  Gmail SMTP  ──►  📧 Daily Email
                                                    
Discord /askfinance  ──►  discord_bot.py  ──►  Claude CLI  ──►  mcp_server.py  ──►  💬 Answer
```

- **Plaid** connects to your bank (any Plaid-supported US bank) and provides your balance and transaction history
- **Claude CLI** is Anthropic's command-line tool. The Discord bot calls it as a subprocess and passes it MCP tools that read your local transaction database — your data never leaves your machine
- **MCP server** (`mcp_server.py`) exposes tools like `get_balance`, `search_transactions`, and `spending_by_category` that Claude uses to answer your questions

---

## What you need

- A Linux machine running 24/7 (a Raspberry Pi, an old mini PC, or a $5/month DigitalOcean VPS all work great)
  - This project was built on a dedicated Ubuntu mini PC but any always-on Linux box works
- Python 3.10+
- A [Plaid](https://plaid.com) developer account (free)
- A Gmail account with an App Password enabled
- A Discord account and a private Discord server
- [Claude Code CLI](https://claude.ai/code) installed and authenticated on your machine
- [Tailscale](https://tailscale.com) (free) — only needed once during Plaid setup

---

## Setup

Work through these steps in order. If you get stuck, paste the error into Claude and ask for help.

### 1. Clone the repo and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/paycheck-tracker.git
cd paycheck-tracker
pip3 install -r requirements.txt
```

### 2. Create your secrets file

Secrets are stored outside the project folder so they can never accidentally be committed to git.

```bash
mkdir -p ~/.config/paycheck-tracker
cp secrets.env.example ~/.config/paycheck-tracker/secrets.env
chmod 600 ~/.config/paycheck-tracker/secrets.env
```

You'll fill in the actual values during the steps below.

### 3. Set up Plaid

Plaid is the service that connects to your bank account.

**Create a Plaid account:**
1. Go to [dashboard.plaid.com](https://dashboard.plaid.com) and sign up for free
2. Create a new app — name it anything
3. Go to **Team Settings → Keys** and copy your **Client ID** and **Sandbox Secret**
4. Run `python3 update_secrets.py` and paste them in when prompted

**Get your bank access token (one-time setup):**

This requires a temporary HTTPS URL. Tailscale Funnel provides one for free.

1. Install Tailscale if you haven't: `curl -fsSL https://tailscale.com/install.sh | sh`
2. Authenticate: `sudo tailscale up`
3. In one terminal, start the Funnel tunnel:
   ```bash
   tailscale funnel 5000
   ```
4. In another terminal, start the setup server:
   ```bash
   python3 setup_plaid.py
   ```
   It will print your redirect URI and a URL to open on your phone — something like:
   ```
   Register this redirect URI: https://your-machine.tailnet-name.ts.net/oauth-redirect
   Open this on your phone: https://your-machine.tailnet-name.ts.net
   ```
5. In your Plaid dashboard go to **Team Settings → API → Allowed redirect URIs** and add the redirect URI it printed
6. Open the phone URL, click through Plaid Link, and connect your bank
7. When it says "Done", your access token is saved automatically to `secrets.env`
8. Remove the redirect URI from Plaid dashboard (it's no longer needed) and stop both terminals

**Switch to production (to use your real bank):**
1. In the Plaid dashboard, request production access (takes a day or two to approve)
2. Once approved, copy your **Production Secret** and run `python3 setup_plaid.py` again with `plaid_env` set to `"production"` in `config.json`
3. Run `python3 update_secrets.py` to save the production secret

### 4. Set up Gmail

You need a Gmail App Password so the script can send email on your behalf.

1. Go to your Google Account → **Security → 2-Step Verification** (must be enabled)
2. Go to **Security → App Passwords** (search for it if you don't see it)
3. Create a new app password — name it "Paycheck Tracker"
4. Copy the 16-character password
5. Run `python3 update_secrets.py` and paste it in when prompted for "Gmail app password"

### 5. Set up Discord bot

**Create the bot:**
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**
2. Name it (e.g. "Finance Bot") and go to the **Bot** tab
3. Click **Reset Token**, copy the token
4. Under **Privileged Gateway Intents**, enable nothing extra is needed for slash commands
5. Go to **OAuth2 → URL Generator**, check `bot` and `applications.commands` scopes, then `Send Messages` permission
6. Copy the generated URL and open it in your browser to invite the bot to your private server

**Find your server ID:**
1. In Discord, go to **Settings → Advanced** and enable **Developer Mode**
2. Right-click your server name and click **Copy Server ID**

**Find your user ID:**
1. Right-click your own username in Discord and click **Copy User ID**

**Save secrets:**
Run `python3 update_secrets.py` and enter the bot token and server ID when prompted.

### 6. Configure config.json

Open `config.json` and update every placeholder:

```json
{
  "plaid_env": "production",
  "gmail": {
    "address": "your.actual.email@gmail.com"
  },
  "recipients": [
    "your.actual.email@gmail.com"
  ],
  "paycheck": {
    "last_friday": "2026-01-02"   ← change to your most recent payday (must be a Friday)
  },
  "low_balance_threshold": 200,   ← warning triggers if safe money drops below this
  "discord": {
    "allowed_user_ids": [YOUR_DISCORD_USER_ID],   ← paste your user ID here
    "known_account_suffixes": ["1234"],            ← last 4 digits of your account number(s)
    "account_holder_name": "FIRST LAST"            ← your name as it appears in transactions
  },
  "claude_path": "/home/YOUR_USERNAME/.local/bin/claude"   ← path to Claude CLI (run: which claude)
}
```

To find your Claude CLI path: `which claude`

### 7. Configure your bills

Edit `bills.json` to list your actual recurring bills. Each entry needs:

```json
{
  "name": "Netflix",      ← name as it appears in your transactions
  "amount": 15.49,        ← expected monthly amount
  "day_of_month": 15,     ← day of month it's charged
  "enabled": true
}
```

You can also run `python3 detect_bills.py` to auto-detect recurring charges from 2 years of transaction history. Review the output carefully before accepting it — it overwrites `bills.json`.

### 8. Configure the MCP server for Claude

Claude needs to know about the MCP server so the Discord bot can use it. Create or edit `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "paycheck-tracker": {
      "command": "python3",
      "args": ["/home/YOUR_USERNAME/paycheck-tracker/mcp_server.py"]
    }
  }
}
```

Replace `YOUR_USERNAME` with your actual Linux username.

### 9. Do an initial transaction sync

Pull your transaction history into the local database:

```bash
python3 sync_transactions.py
```

This may take a minute. On first run it fetches everything available (usually 2 years).

### 10. Test the daily email

Run it manually to make sure everything works:

```bash
python3 main.py
```

Check your inbox. If it worked, you'll get the daily summary email.

### 11. Schedule the daily email with cron

```bash
crontab -e
```

Add this line (sends the email every day at 8 AM):

```
0 8 * * * python3 /home/YOUR_USERNAME/paycheck-tracker/main.py >> /home/YOUR_USERNAME/paycheck-tracker/logs/daily.log 2>&1
```

### 12. Run the Discord bot as a background service

This uses systemd so the bot restarts automatically if it crashes or if the machine reboots.

```bash
# Copy the service file (edit YOUR_USERNAME first)
nano finance-bot.service  # replace every YOUR_USERNAME with your actual username
sudo cp finance-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable finance-bot
sudo systemctl start finance-bot
```

Check that it's running:

```bash
sudo systemctl status finance-bot
```

---

## Using the Discord bot

In your private Discord server, type:

```
/askfinance how much did I spend on food last month?
/askfinance what are my upcoming bills?
/askfinance show me my recent transactions at Walmart
/askfinance am I on track with my budget this month?
```

Claude will call into your local transaction database and respond with real data. No financial data is sent to Discord — Claude processes it locally and only sends you the text answer.

---

## Keeping transactions up to date

The MCP server reads from your local `transactions.db`. To keep it fresh, add a second cron job to sync daily:

```
30 7 * * * python3 /home/YOUR_USERNAME/paycheck-tracker/sync_transactions.py >> /home/YOUR_USERNAME/paycheck-tracker/logs/daily.log 2>&1
```

This runs at 7:30 AM, 30 minutes before the daily email, so the email always has fresh data.

---

## Refreshing your bill list

Run `detect_bills.py` a few times a year (January, May, September are good) to pick up new subscriptions:

```bash
python3 detect_bills.py
```

Note: it needs at least 10 months of consistent charges to detect a bill, and it overwrites `bills.json` entirely. Review the output before accepting it and re-add any bills you want to keep that weren't detected.

---

## File overview

| File | What it does |
|------|-------------|
| `main.py` | Daily email runner — fetches balance, calculates safe money, sends email |
| `sync_transactions.py` | Syncs Plaid transactions to local SQLite database |
| `mcp_server.py` | MCP server exposing financial query tools to Claude |
| `discord_bot.py` | Discord bot — receives `/askfinance` commands and calls Claude |
| `plaid_client.py` | Shared Plaid API client factory |
| `setup_plaid.py` | One-time OAuth flow to connect your bank account |
| `detect_bills.py` | Auto-detects recurring bills from transaction history |
| `update_secrets.py` | Interactive prompt to update credentials in secrets.env |
| `config.json` | Your configuration (email, payday, Discord settings, etc.) |
| `bills.json` | Your known recurring bills |
| `finance-bot.service` | systemd service file for running the Discord bot |
| `secrets.env.example` | Template showing what credentials you need |

Backups of your config files before major changes are a good idea — `cp config.json config.json.bak` before editing.
