"""Deliver alerts via Slack and/or email (Resend)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import IntFlag, auto

from src.config import Config
from src.data_store import DataStore

logger = logging.getLogger(__name__)


class DeliveryChannel(IntFlag):
    NONE = 0
    SLACK = auto()
    EMAIL = auto()


@dataclass
class AlertMessage:
    alert_type: str
    subject: str
    body: str  # markdown
    ticker: str | None = None


class Alerter:
    """Routes alerts to configured delivery channels and logs them to the DB."""

    def __init__(self, cfg: Config, store: DataStore) -> None:
        self._cfg = cfg
        self._store = store

    # ── Public ─────────────────────────────────────────────────────────────────

    def send(self, alert: AlertMessage) -> DeliveryChannel:
        """Send to all configured channels, return bitmask of successes."""
        delivered = DeliveryChannel.NONE

        if self._cfg.slack_enabled:
            if self._send_slack(alert):
                delivered |= DeliveryChannel.SLACK

        if self._cfg.email_enabled:
            if self._send_email(alert):
                delivered |= DeliveryChannel.EMAIL

        self._store.log_alert(
            alert_type=alert.alert_type,
            message=alert.body,
            ticker=alert.ticker,
            delivered=int(delivered),
        )
        return delivered

    def send_daily_digest(self, markdown_content: str) -> DeliveryChannel:
        alert = AlertMessage(
            alert_type="daily_digest",
            subject=f"Portfolio Digest – {datetime.utcnow().strftime('%Y-%m-%d')}",
            body=markdown_content,
        )
        return self.send(alert)

    def send_drift_alert(self, summary: str, drifted: list[dict]) -> DeliveryChannel:
        lines = [f"⚠️ **Portfolio Drift Detected**\n", summary, ""]
        if drifted:
            lines.append("| Ticker | Current % | Previous % | Delta % | Action |")
            lines.append("|--------|-----------|------------|---------|--------|")
            for p in drifted:
                lines.append(
                    f"| {p.get('ticker','-')} "
                    f"| {p.get('current_pct',0):.1f}% "
                    f"| {p.get('previous_pct',0):.1f}% "
                    f"| {p.get('delta_pct',0):+.1f}% "
                    f"| {p.get('action','-')} |"
                )
        alert = AlertMessage(
            alert_type="drift_alert",
            subject="⚠️ Portfolio Drift Alert",
            body="\n".join(lines),
        )
        return self.send(alert)

    def send_loss_alert(self, message: str) -> DeliveryChannel:
        alert = AlertMessage(
            alert_type="daily_loss_alert",
            subject="🔴 Portfolio Daily Loss Alert",
            body=f"🔴 **Daily Loss Threshold Breached**\n\n{message}",
        )
        return self.send(alert)

    # ── Slack ──────────────────────────────────────────────────────────────────

    def _send_slack(self, alert: AlertMessage) -> bool:
        try:
            from slack_sdk import WebClient
            from slack_sdk.errors import SlackApiError

            client = WebClient(token=self._cfg.slack_bot_token)
            # Convert markdown body to Slack mrkdwn (best-effort)
            text = _md_to_slack(alert.body)
            client.chat_postMessage(
                channel=self._cfg.slack_channel,
                text=f"*{alert.subject}*\n\n{text}",
                mrkdwn=True,
            )
            logger.info("Slack alert sent: %s", alert.alert_type)
            return True
        except ImportError:
            logger.warning("slack_sdk not installed — skipping Slack delivery")
            return False
        except Exception as exc:
            logger.error("Slack delivery failed: %s", exc)
            return False

    # ── Email / Resend ─────────────────────────────────────────────────────────

    def _send_email(self, alert: AlertMessage) -> bool:
        try:
            import resend

            resend.api_key = self._cfg.resend_api_key
            html = _md_to_html(alert.body)
            resend.Emails.send(
                {
                    "from": self._cfg.email_from,
                    "to": self._cfg.email_to,
                    "subject": alert.subject,
                    "html": html,
                }
            )
            logger.info("Email sent: %s", alert.alert_type)
            return True
        except ImportError:
            logger.warning("resend not installed — skipping email delivery")
            return False
        except Exception as exc:
            logger.error("Email delivery failed: %s", exc)
            return False


# ── Formatting helpers ────────────────────────────────────────────────────────

def _md_to_slack(text: str) -> str:
    """Minimal markdown → Slack mrkdwn conversion."""
    import re

    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Headings: ## Heading → *Heading*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return text


def _md_to_html(text: str) -> str:
    """Minimal markdown → HTML. Falls back to <pre> if markdown lib unavailable."""
    try:
        import markdown as md

        return md.markdown(text, extensions=["tables"])
    except ImportError:
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre style='font-family: monospace;'>{escaped}</pre>"
