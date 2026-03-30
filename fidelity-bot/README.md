# Fidelity Portfolio Intelligence Bot

AI-powered portfolio monitoring, autonomous research, and gains-optimised allocation — powered by **Plaid** (live Fidelity data), **Claude Opus 4.6** (AI analysis), and a **Karpathy-style autonomous research loop**.

---

## How it works in 30 seconds

```
Fidelity account
      │  read-only, via Plaid API
      ▼
portfolio-bot fetch          ← pulls holdings + transactions → SQLite + JSON
      │
      ▼
portfolio-bot research       ← 3-phase autonomous research loop:
      │                         1. Plan    — Claude picks tickers & generates questions
      │                         2. Research — per-ticker + macro analysis → signals
      │                         3. Synthesise — ranked opportunities, risks, rebalancing plan
      ▼
portfolio-bot optimize       ← gains-maximising allocation via Kelly criterion
      │                         blends research signals into expected-return estimates
      ▼
Slack / email digest         ← gains-focused brief with specific action items
```

> **You never manually "dump" your portfolio.**
> The bot connects directly to Fidelity via Plaid and pulls your holdings automatically each time you run `fetch` (or on the automated daily schedule). Plaid is **read-only** — it cannot place trades.

---

## Prerequisites

| Requirement | Where to get it | Notes |
|-------------|----------------|-------|
| Python 3.11+ | [python.org](https://python.org) | `python --version` to check |
| Plaid account | [dashboard.plaid.com](https://dashboard.plaid.com) | Free developer tier is enough |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) | Claude Opus 4.6 |
| Slack bot token | [api.slack.com/apps](https://api.slack.com/apps) | **Optional** — for Slack alerts |
| Resend API key | [resend.com](https://resend.com) | **Optional** — for email alerts |

---

## Step 1 — Install

```bash
git clone <repo-url>
cd fidelity-bot

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install
pip install -e .

# Verify
portfolio-bot --version
# portfolio-bot, version 0.1.0
```

---

## Step 2 — Get Plaid credentials

Plaid is the service that securely connects the bot to your Fidelity account.

1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com) (free).
2. Create a new app — name it anything (e.g. "Portfolio Bot").
3. Go to **Team Settings → Keys** and copy:
   - `client_id`
   - `secret`  ← use the **Sandbox** secret first to test with fake data

### Sandbox vs Production

| Mode | Data | Cost | When to use |
|------|------|------|-------------|
| `sandbox` | Synthetic test data | Free | First-time setup and testing |
| `development` | Real accounts, ≤100 items | Free | Personal use |
| `production` | Real accounts, unlimited | Paid per item | At scale |

**Start with `sandbox` to make sure everything works before touching your real account.**

---

## Step 3 — Connect your Fidelity account and get an access token

This is a one-time step per brokerage account.

### Option A — Sandbox (fake data, no Fidelity login needed)

Sandbox gives you synthetic portfolio data to test the bot end-to-end without connecting a real account.

1. Run `portfolio-bot link` — it prints a `link_token`.
2. Use Plaid's sandbox flow to exchange it. The simplest way is via the [Plaid Quickstart](https://plaid.com/docs/quickstart/) hosted demo, or with a single curl call:

```bash
# Exchange a sandbox public token for an access token
curl -s -X POST https://sandbox.plaid.com/item/public_token/exchange \
  -H 'Content-Type: application/json' \
  -d '{
    "client_id": "YOUR_CLIENT_ID",
    "secret":    "YOUR_SANDBOX_SECRET",
    "public_token": "public-sandbox-XXXX"
  }' | python -m json.tool
# Returns: {"access_token": "access-sandbox-XXXX", "item_id": "..."}
```

Alternatively, Plaid provides a pre-built sandbox access token for testing — check the [Plaid sandbox docs](https://plaid.com/docs/sandbox/) for the current test credentials.

Copy the `access_token` value into your `.env` as `PLAID_ACCESS_TOKENS`.

### Option B — Real Fidelity account (development/production)

1. Run `portfolio-bot link` — copy the printed `link_token`.
2. Load the [Plaid Link](https://plaid.com/docs/link/) UI in a browser, passing your `link_token`. The simplest approach is the Plaid Quickstart app (clone and run locally in 2 minutes).
3. In the browser popup, search for **Fidelity Investments** and log in with your Fidelity credentials.
4. Plaid returns a `public_token` (short-lived, ~30 minutes). Exchange it:

```bash
curl -s -X POST https://production.plaid.com/item/public_token/exchange \
  -H 'Content-Type: application/json' \
  -d '{
    "client_id": "YOUR_CLIENT_ID",
    "secret":    "YOUR_PRODUCTION_SECRET",
    "public_token": "public-production-XXXX"
  }' | python -m json.tool
# Returns: {"access_token": "access-production-XXXX", "item_id": "..."}
```

Save the `access_token` — this is your permanent Fidelity connection key.

> **Multiple accounts** (e.g. individual brokerage + IRA):
> Repeat the link flow once per account. Add all `access_token` values to `.env`, comma-separated.

---

## Step 4 — Configure `.env`

```bash
cp .env.example .env
# Open .env in your editor and fill in the values
```

Minimum required configuration:

```bash
# Plaid
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_secret
PLAID_ENV=sandbox                     # or: development / production
PLAID_ACCESS_TOKENS=access-sandbox-xxxx-xxxx-xxxx

# Claude
ANTHROPIC_API_KEY=sk-ant-xxxx
```

Full reference — add these as needed:

```bash
# Slack alerts (leave blank to disable)
SLACK_BOT_TOKEN=xoxb-xxxx
SLACK_CHANNEL=#portfolio-alerts

# Email alerts via Resend (leave blank to disable)
RESEND_API_KEY=re_xxxx
EMAIL_FROM=bot@yourdomain.com
EMAIL_TO=you@yourdomain.com

# Storage paths (defaults shown)
DB_PATH=./data/portfolio.db
SNAPSHOTS_DIR=./data/snapshots

# Alert thresholds
ALERT_DRIFT_THRESHOLD=0.05            # alert if a position shifts >5%
ALERT_DAILY_LOSS_THRESHOLD=0.02       # alert if portfolio drops >2% in one day

# Daily scheduler time (ET)
SCHEDULER_CRON_HOUR=18
SCHEDULER_CRON_MINUTE=0

# Auto-research (Karpathy-style)
RESEARCH_ENABLED=true
RESEARCH_MAX_TICKERS=5               # deeper = more thorough, more API tokens
```

---

## Step 5 — Pull your portfolio for the first time

```bash
portfolio-bot fetch
```

This calls Plaid, downloads your current holdings and the last 30 days of transactions, and saves everything locally.

```
┌─────────────────────────────────┐
│       Portfolio Snapshot        │
├──────────────┬──────────────────┤
│ Snapshot ID  │ 1                │
│ Fetched At   │ 2026-03-30 18:00 │
│ Accounts     │ 2                │
│ Holdings     │ 47               │
│ Transactions │ 83               │
│ Total Value  │ $124,532.18      │
└──────────────┴──────────────────┘
```

Run this command every day (or use the scheduler) to build the historical data the optimiser needs.

---

## All CLI commands

### `fetch` — pull data from Fidelity

```bash
portfolio-bot fetch                   # default: 30 days of transactions
portfolio-bot fetch --txn-days 90     # pull 90 days of transactions
```

### `holdings` — view current positions

```bash
portfolio-bot holdings                # sorted by value (largest first)
portfolio-bot holdings --sort gain    # sorted by unrealised gain
portfolio-bot holdings --sort ticker  # alphabetical
portfolio-bot holdings --limit 10     # show top 10 only
```

### `research` — full autonomous research loop ⭐ recommended

```bash
portfolio-bot research
```

Runs the **three-phase Karpathy research loop** on your top positions:

1. **Plan** — Claude reads your snapshot and autonomously decides which positions most need research, generating 2–4 specific questions per ticker (fundamentals, sector context, macro factors, risk)
2. **Research** — Claude answers each question from its training knowledge (up to Aug 2025), producing a `BUY MORE / HOLD / TRIM / SELL` signal with a confidence score per ticker, plus macro cross-portfolio analysis
3. **Synthesise** — findings are combined into ranked opportunities, top risks, and a rebalancing plan

The **gains-maximising optimiser** runs automatically after, using research signals to compute target weights via Kelly criterion.

Finally, Claude produces a **unified research-backed digest** combining everything into a gains-focused action plan.

```bash
portfolio-bot research --max-tickers 3    # faster/cheaper: 3 tickers
portfolio-bot research --max-tickers 10   # thorough: 10 tickers
portfolio-bot research --send             # also send to Slack/email
portfolio-bot research --show-thinking    # print Claude's step-by-step reasoning
```

**API cost estimate:** ~$0.15–$0.30 per run at 5 tickers (Opus 4.6 pricing).

### `optimize` — gains-maximising allocation

```bash
portfolio-bot optimize                            # Kelly + momentum, no research
portfolio-bot optimize --with-research            # run research first, blend signals
portfolio-bot optimize --max-position 0.20        # cap any single position at 20%
portfolio-bot optimize --kelly-fraction 0.25      # conservative half-Kelly
portfolio-bot optimize --with-research --max-tickers 5
```

**How the optimiser works:**

| Step | What it does |
|------|-------------|
| 1 | Pulls value history per ticker from `holdings_history` SQLite table |
| 2 | Computes mean return and volatility per period |
| 3 | Kelly fraction: `f* = mean_return / volatility²` × kelly_fraction (default 0.5) |
| 4 | Momentum score: recent 3-period return ÷ full-history return |
| 5 | Research overlay: multiplies expected returns by signal (`buy_more=1.3×`, `hold=1.0×`, `trim=0.6×`, `sell=0×`) |
| 6 | Normalises weights with a 30% concentration cap, iteratively redistributing excess |
| 7 | Outputs rebalancing table sorted by largest required move |

> **Important:** The optimiser needs at least 2–3 weeks of daily `fetch` runs to produce reliable return estimates. On first use it will warn about sparse data and fall back to equal-weight or research-signal-only allocation. Run `fetch` daily to build history.

### `digest` — standard daily AI digest

```bash
portfolio-bot digest                  # print to terminal
portfolio-bot digest --send           # print + send to Slack/email
portfolio-bot digest --show-thinking  # also show Claude's reasoning
```

Analyses the two most recent snapshots: biggest movers, dead weight, top 3 action items.

### `ask` — ad hoc questions

```bash
portfolio-bot ask "What is my largest position?"
portfolio-bot ask "How much am I exposed to tech stocks?"
portfolio-bot ask "Which positions have the worst unrealised losses?"
portfolio-bot ask "Should I be concerned about concentration risk?"
portfolio-bot ask "Show me all my trades in the last 30 days"
portfolio-bot ask "What is my total unrealised gain?"
```

### `check-alerts` — drift and loss threshold checks

```bash
portfolio-bot check-alerts            # print results to terminal
portfolio-bot check-alerts --send     # send alerts if thresholds are breached
```

Checks:
- **Drift alert** — any position that shifted >5% of portfolio allocation since the last snapshot (configurable via `ALERT_DRIFT_THRESHOLD`)
- **Daily loss alert** — total portfolio dropped >2% since the last snapshot (configurable via `ALERT_DAILY_LOSS_THRESHOLD`)

### `alerts-log` — view alert history

```bash
portfolio-bot alerts-log
portfolio-bot alerts-log --limit 20
```

### `link` — generate a Plaid Link token

```bash
portfolio-bot link
# Prints a link_token to use with Plaid's browser UI
```

---

## Automated daily schedule

Run the scheduler as a background process to automate the entire pipeline:

```bash
python scheduler.py
```

At **6 PM ET daily** (configurable), it runs:

```
1. fetch           → pull fresh holdings from Plaid
2. check_drift     → send drift alert if a position shifted >5%
3. check_loss      → send loss alert if portfolio dropped >2%
4. research        → 3-phase Karpathy research loop (if RESEARCH_ENABLED=true)
5. optimize        → gains-maximising allocation with research overlay
6. research_digest → unified digest sent to Slack/email
```

If research fails for any reason, it falls back to the standard digest automatically.

### Run as a systemd service (Linux)

```ini
# /etc/systemd/system/portfolio-bot.service
[Unit]
Description=Portfolio Intelligence Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/fidelity-bot
ExecStart=/path/to/fidelity-bot/.venv/bin/python scheduler.py
Restart=always
EnvironmentFile=/path/to/fidelity-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now portfolio-bot
sudo journalctl -u portfolio-bot -f     # follow logs
```

### Keep alive with screen (simple option)

```bash
screen -S portfolio-bot
cd fidelity-bot && source .venv/bin/activate
python scheduler.py
# Ctrl+A, D  to detach (bot keeps running in background)
# screen -r portfolio-bot  to reattach
```

---

## Where your data lives

```
fidelity-bot/
└── data/
    ├── portfolio.db                       ← SQLite database
    │   ├── snapshots table                ← every fetch (JSON blob + metadata)
    │   ├── holdings_history table         ← per-ticker time-series (for optimiser)
    │   └── alerts_log table               ← every alert sent
    └── snapshots/
        ├── snapshot_20260330_180000.json  ← full point-in-time snapshot
        ├── snapshot_20260331_180000.json
        └── ...
```

### Inspect the database directly

```bash
sqlite3 data/portfolio.db

-- Most recent 5 snapshots
SELECT fetched_at, total_value FROM snapshots ORDER BY fetched_at DESC LIMIT 5;

-- AAPL value history
SELECT fetched_at, institution_value FROM holdings_history
WHERE ticker='AAPL' ORDER BY fetched_at;

-- Recent alerts
SELECT created_at, alert_type, ticker, message
FROM alerts_log ORDER BY created_at DESC LIMIT 10;
```

### Snapshot JSON format

Each `data/snapshots/snapshot_*.json` file looks like:

```json
{
  "fetched_at": "2026-03-30T18:00:00",
  "total_value": "124532.18",
  "accounts": [
    { "account_id": "...", "name": "Fidelity Individual", "type": "investment" }
  ],
  "holdings": [
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "quantity": "50.0",
      "institution_price": "175.42",
      "institution_value": "8771.00",
      "cost_basis": "6500.00",
      "unrealised_gain": "2271.00",
      "unrealised_gain_pct": "34.94"
    }
  ],
  "transactions": [
    {
      "ticker": "MSFT",
      "date": "2026-03-25",
      "type": "buy",
      "quantity": "5.0",
      "amount": "1987.50",
      "price": "397.50"
    }
  ]
}
```

---

## Recommended daily workflow

```bash
# Pull fresh data (do this every day to build history for the optimiser)
portfolio-bot fetch

# Full research + optimisation + digest (replaces the simple digest)
portfolio-bot research --send

# After a big market move, check thresholds immediately
portfolio-bot check-alerts --send

# Before you rebalance, run the optimiser with research signals
portfolio-bot optimize --with-research

# Ask Claude anything
portfolio-bot ask "Is my tech exposure too high right now?"
portfolio-bot ask "What are my top 3 positions by unrealised gain?"
```

Or just configure the scheduler and get everything automatically at 6 PM.

---

## Troubleshooting

**`No PLAID_ACCESS_TOKENS configured`**
→ Run `portfolio-bot link`, complete the Plaid Link browser flow, then add the `access_token` to `.env`.

**`No portfolio data available. Run portfolio-bot fetch first.`**
→ `portfolio-bot fetch` must be run at least once before any analysis commands.

**Optimiser warning: "insufficient history, fell back to equal-weight"**
→ Normal on first use. Run `portfolio-bot fetch` daily for 1–2 weeks to build return history.

**Plaid error: `PRODUCTS_NOT_SUPPORTED`**
→ Your Plaid app needs the `investments` product enabled. Check your app at [dashboard.plaid.com](https://dashboard.plaid.com) → Products.

**Plaid sandbox auth errors**
→ Re-run `portfolio-bot link` and redo the token exchange. Sandbox item connections can expire.

**Claude API timeout during research**
→ Lower `--max-tickers` to 2 or 3. Each ticker research call is independent and uses ~3k tokens.

**Scheduler not firing**
→ Check timezone: `SCHEDULER_CRON_HOUR` is in US/Eastern time. Check logs: `python scheduler.py` logs to stdout.

---

## Security notes

- Plaid access is **read-only** — this bot cannot place trades, transfer funds, or modify your account
- Never commit `.env`, `*.db`, or snapshot files (all in `.gitignore`)
- Treat `PLAID_ACCESS_TOKENS` like passwords — rotate if exposed
- Use `PLAID_ENV=sandbox` for all testing; only use `production` when ready
- All data stays local; nothing is sent to external services except Plaid API, Anthropic API, and your configured alert channels (Slack/Resend)

---

## Environment variable reference

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `PLAID_CLIENT_ID` | — | yes | Plaid app client ID |
| `PLAID_SECRET` | — | yes | Plaid app secret |
| `PLAID_ENV` | `sandbox` | yes | `sandbox` / `development` / `production` |
| `PLAID_ACCESS_TOKENS` | — | yes | Comma-separated Plaid access tokens |
| `ANTHROPIC_API_KEY` | — | yes | Claude API key |
| `SLACK_BOT_TOKEN` | — | no | Slack bot token (alerts off if blank) |
| `SLACK_CHANNEL` | `#portfolio-alerts` | no | Slack target channel |
| `RESEND_API_KEY` | — | no | Resend API key (email off if blank) |
| `EMAIL_FROM` | — | no | Sender address |
| `EMAIL_TO` | — | no | Recipient address |
| `DB_PATH` | `./data/portfolio.db` | no | SQLite database path |
| `SNAPSHOTS_DIR` | `./data/snapshots` | no | JSON snapshot directory |
| `ALERT_DRIFT_THRESHOLD` | `0.05` | no | Position drift alert (5%) |
| `ALERT_DAILY_LOSS_THRESHOLD` | `0.02` | no | Daily loss alert (2%) |
| `SCHEDULER_CRON_HOUR` | `18` | no | Hour (ET) for daily job |
| `SCHEDULER_CRON_MINUTE` | `0` | no | Minute for daily job |
| `RESEARCH_ENABLED` | `true` | no | Run auto-research in daily scheduler |
| `RESEARCH_MAX_TICKERS` | `5` | no | Tickers to research per run |

---

## Development

```bash
pip install -e ".[dev]"
ruff check src/
pytest tests/
```
