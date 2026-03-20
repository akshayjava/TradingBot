"""Load and validate environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Config:
    # ── Plaid ──────────────────────────────────────────────────────────────────
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str  # sandbox | development | production
    plaid_access_tokens: list[str]

    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str

    # ── Slack (optional) ───────────────────────────────────────────────────────
    slack_bot_token: str
    slack_channel: str

    # ── Email / Resend (optional) ──────────────────────────────────────────────
    resend_api_key: str
    email_from: str
    email_to: str

    # ── App ────────────────────────────────────────────────────────────────────
    db_path: Path
    snapshots_dir: Path
    alert_drift_threshold: float
    alert_daily_loss_threshold: float
    scheduler_cron_hour: int
    scheduler_cron_minute: int

    @classmethod
    def from_env(cls) -> "Config":
        raw_tokens = _optional("PLAID_ACCESS_TOKENS", "")
        access_tokens = [t.strip() for t in raw_tokens.split(",") if t.strip()]

        return cls(
            plaid_client_id=_require("PLAID_CLIENT_ID"),
            plaid_secret=_require("PLAID_SECRET"),
            plaid_env=_optional("PLAID_ENV", "sandbox"),
            plaid_access_tokens=access_tokens,
            anthropic_api_key=_require("ANTHROPIC_API_KEY"),
            slack_bot_token=_optional("SLACK_BOT_TOKEN"),
            slack_channel=_optional("SLACK_CHANNEL", "#portfolio-alerts"),
            resend_api_key=_optional("RESEND_API_KEY"),
            email_from=_optional("EMAIL_FROM"),
            email_to=_optional("EMAIL_TO"),
            db_path=Path(_optional("DB_PATH", "./data/portfolio.db")),
            snapshots_dir=Path(_optional("SNAPSHOTS_DIR", "./data/snapshots")),
            alert_drift_threshold=float(_optional("ALERT_DRIFT_THRESHOLD", "0.05")),
            alert_daily_loss_threshold=float(_optional("ALERT_DAILY_LOSS_THRESHOLD", "0.02")),
            scheduler_cron_hour=int(_optional("SCHEDULER_CRON_HOUR", "18")),
            scheduler_cron_minute=int(_optional("SCHEDULER_CRON_MINUTE", "0")),
        )

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token)

    @property
    def email_enabled(self) -> bool:
        return bool(self.resend_api_key and self.email_from and self.email_to)


# Module-level singleton — lazy-loaded on first import of config
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config
