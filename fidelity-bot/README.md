# Fidelity Portfolio Intelligence Bot

An AI-powered portfolio monitoring system that connects to your Fidelity brokerage account via **Plaid**, analyses your holdings with **Claude Opus 4.6**, and delivers actionable alerts to **Slack** and/or **email**.

## Architecture

```
Fidelity Account
      │
      ▼
   Plaid API (read-only: holdings, balances, transactions)
      │
      ▼
   Python Data Pipeline
   ├── src/plaid_client.py   → Plaid API wrapper
   ├── src/data_fetcher.py   → normalised data structures
   ├── src/data_store.py     → SQLite + JSON snapshots
   ├── src/analyzer.py       → Claude API analysis (adaptive thinking)
   ├── src/alerter.py        → Slack / Resend email delivery
   └── src/cli.py            → interactive CLI
      │
      ▼
   Outputs: CLI, Slack alerts, daily digest emails
```

## Quick Start

### 1. Install

```bash
# Python 3.11+ required
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials (see Configuration section)
```

### 3. Link a Fidelity Account

```bash
# Generate a Plaid Link token
portfolio-bot link
# Use the token in Plaid's Link UI to connect Fidelity
# Store the resulting access_token in PLAID_ACCESS_TOKENS in .env
```

### 4. Fetch Portfolio Data

```bash
portfolio-bot fetch
```

### 5. View Holdings

```bash
portfolio-bot holdings
portfolio-bot holdings --sort gain
```

### 6. Get AI Analysis

```bash
# Daily digest
portfolio-bot digest

# Ask any question
portfolio-bot ask "What is my largest position and how has it performed?"
portfolio-bot ask "Am I too concentrated in tech stocks?"
portfolio-bot ask "Show me all trades in the last 30 days"

# Check drift/loss thresholds
portfolio-bot check-alerts
```

### 7. Send Alerts

```bash
# Send digest via Slack/email
portfolio-bot digest --send

# Check thresholds and send if breached
portfolio-bot check-alerts --send
```

## Automated Scheduling

Run the scheduler as a background service (fires daily at 6 PM ET by default):

```bash
python scheduler.py
```

Or use `systemd`, `supervisord`, or Docker to keep it running.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Description |
|----------|----------|-------------|
| `PLAID_CLIENT_ID` | ✅ | Plaid dashboard client ID |
| `PLAID_SECRET` | ✅ | Plaid dashboard secret |
| `PLAID_ENV` | ✅ | `sandbox` / `development` / `production` |
| `PLAID_ACCESS_TOKENS` | ✅ | Comma-separated access tokens from Plaid Link |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key |
| `SLACK_BOT_TOKEN` | optional | Slack bot OAuth token |
| `SLACK_CHANNEL` | optional | Slack channel name (default `#portfolio-alerts`) |
| `RESEND_API_KEY` | optional | Resend email API key |
| `EMAIL_FROM` | optional | Sender address |
| `EMAIL_TO` | optional | Recipient address |
| `DB_PATH` | optional | SQLite path (default `./data/portfolio.db`) |
| `SNAPSHOTS_DIR` | optional | JSON snapshots dir (default `./data/snapshots`) |
| `ALERT_DRIFT_THRESHOLD` | optional | Absolute drift % to trigger alert (default `0.05`) |
| `ALERT_DAILY_LOSS_THRESHOLD` | optional | Daily loss % to trigger alert (default `0.02`) |
| `SCHEDULER_CRON_HOUR` | optional | Hour for daily digest (default `18` = 6 PM) |
| `SCHEDULER_CRON_MINUTE` | optional | Minute for daily digest (default `0`) |

## CLI Reference

```
portfolio-bot fetch              Pull latest data from Plaid
portfolio-bot holdings           Show current holdings table
portfolio-bot digest             Run daily AI digest (--send to deliver)
portfolio-bot ask "..."          Ask Claude any question about your portfolio
portfolio-bot check-alerts       Check drift/loss thresholds (--send to deliver)
portfolio-bot link               Generate a Plaid Link token
portfolio-bot alerts-log         View recent alert history
```

## AI Model

Uses **Claude Opus 4.6** with **adaptive thinking** enabled. Claude dynamically decides how much to reason internally before answering, producing higher-quality portfolio analysis without a fixed token budget.

## Data Storage

- **SQLite** (`data/portfolio.db`) — holdings history, alert log, snapshot metadata
- **JSON snapshots** (`data/snapshots/`) — full point-in-time payloads for each fetch
- All data is stored locally; nothing is sent to external services except the Plaid API, Anthropic API, and your configured alert channels

## Security Notes

- Plaid access is **read-only** — the bot cannot execute trades
- Never commit `.env` or `*.db` files (both are in `.gitignore`)
- Store `PLAID_ACCESS_TOKENS` securely; treat them like passwords
- Use `PLAID_ENV=sandbox` for testing with synthetic data

## Development

```bash
pip install -e ".[dev]"
ruff check src/
pytest tests/
```
