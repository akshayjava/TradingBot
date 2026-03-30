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
    """Fetch data → auto-research → optimise → check alerts → send gains-focused digest."""
    logger.info("=== Daily portfolio job starting ===")
    try:
        from src.alerter import Alerter
        from src.analyzer import PortfolioAnalyzer
        from src.auto_researcher import AutoResearcher
        from src.config import get_config
        from src.data_fetcher import DataFetcher
        from src.data_store import DataStore
        from src.optimizer import PortfolioOptimizer
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

        # 2. Check for threshold breaches (fast — no LLM needed)
        drift = analyzer.check_drift()
        if drift and drift.alert_required:
            logger.info("Drift alert triggered: %s", drift.summary)
            alerter.send_drift_alert(drift.summary, drift.drifted_positions)

        loss_triggered, loss_msg = analyzer.check_daily_loss()
        if loss_triggered:
            logger.info("Daily loss alert triggered: %s", loss_msg)
            alerter.send_loss_alert(loss_msg)

        # 3. Auto-research (Karpathy-style) + gains-maximising optimisation
        research_report_md = ""
        if cfg.research_enabled:
            logger.info("Running auto-research (max %d tickers)…", cfg.research_max_tickers)
            try:
                researcher = AutoResearcher(cfg, store)
                report = researcher.conduct_research(max_tickers=cfg.research_max_tickers)
                research_report_md = researcher.format_report_markdown(report)
                logger.info(
                    "Research complete — %d tickers researched, %d input / %d output tokens",
                    len(report.tickers_researched),
                    report.total_input_tokens,
                    report.total_output_tokens,
                )

                # Run optimizer with research overlay
                optimizer = PortfolioOptimizer(store)
                opt_result = optimizer.optimize(research_report=report)
                opt_md = optimizer.format_result_markdown(opt_result)
                logger.info(
                    "Optimisation complete — %d rebalancing actions, "
                    "expected gain: %+.3f%% per period",
                    len(opt_result.rebalancing_actions),
                    opt_result.expected_portfolio_gain * 100,
                )
                # Append optimisation table to research report
                research_report_md += "\n\n" + opt_md

            except Exception as research_exc:
                logger.warning("Auto-research failed (non-fatal): %s", research_exc)

        # 4. Send gains-focused digest
        if research_report_md:
            digest = analyzer.research_digest(research_report_md)
        else:
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
