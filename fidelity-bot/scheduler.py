"""APScheduler-based cron runner for automated portfolio checks."""

from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scheduler")


def run_daily_job():
    """Fetch data → check alerts → send daily digest."""
    logger.info("=== Daily portfolio job starting ===")
    try:
        from src.alerter import Alerter
        from src.analyzer import PortfolioAnalyzer
        from src.config import get_config
        from src.data_fetcher import DataFetcher
        from src.data_store import DataStore
        from src.plaid_client import PlaidClient

        cfg = get_config()
        store = DataStore(cfg.db_path, cfg.snapshots_dir)

        # 1. Fetch fresh data
        if cfg.plaid_access_tokens:
            plaid = PlaidClient(cfg)
            fetcher = DataFetcher(plaid)
            snapshot = fetcher.fetch_snapshot(txn_days=30)
            store.save_snapshot(snapshot)
            logger.info("Snapshot saved — total value: $%s", snapshot.total_value)
        else:
            logger.warning("No Plaid access tokens configured — skipping fetch")

        analyzer = PortfolioAnalyzer(cfg, store)
        alerter = Alerter(cfg, store)

        # 2. Check for threshold breaches
        drift = analyzer.check_drift()
        if drift and drift.alert_required:
            logger.info("Drift alert triggered: %s", drift.summary)
            alerter.send_drift_alert(drift.summary, drift.drifted_positions)

        loss_triggered, loss_msg = analyzer.check_daily_loss()
        if loss_triggered:
            logger.info("Daily loss alert triggered: %s", loss_msg)
            alerter.send_loss_alert(loss_msg)

        # 3. Send daily digest
        digest = analyzer.daily_digest()
        alerter.send_daily_digest(digest.content)
        logger.info(
            "Daily digest sent — %d input tokens / %d output tokens",
            digest.input_tokens,
            digest.output_tokens,
        )

    except Exception as exc:
        logger.exception("Daily job failed: %s", exc)

    logger.info("=== Daily portfolio job complete ===")


def main():
    from src.config import get_config

    cfg = get_config()

    scheduler = BlockingScheduler(timezone="America/New_York")
    trigger = CronTrigger(
        hour=cfg.scheduler_cron_hour,
        minute=cfg.scheduler_cron_minute,
        timezone="America/New_York",
    )
    scheduler.add_job(run_daily_job, trigger, id="daily_portfolio_job", name="Daily Digest")

    logger.info(
        "Scheduler started — daily job at %02d:%02d ET",
        cfg.scheduler_cron_hour,
        cfg.scheduler_cron_minute,
    )
    logger.info("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
