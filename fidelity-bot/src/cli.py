"""Interactive CLI for the Fidelity Portfolio Intelligence Bot."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from src.config import get_config
from src.data_fetcher import DataFetcher
from src.data_store import DataStore
from src.plaid_client import PlaidClient

console = Console()


def _make_deps():
    cfg = get_config()
    store = DataStore(cfg.db_path, cfg.snapshots_dir)
    plaid = PlaidClient(cfg)
    fetcher = DataFetcher(plaid)
    return cfg, store, plaid, fetcher


# ── Root group ────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("0.1.0", prog_name="portfolio-bot")
def cli():
    """Fidelity Portfolio Intelligence Bot — powered by Plaid + Claude."""


# ── fetch ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--txn-days", default=30, show_default=True, help="Days of transactions to fetch.")
def fetch(txn_days: int):
    """Pull latest portfolio data from Plaid and store it locally."""
    cfg, store, plaid, fetcher = _make_deps()

    if not cfg.plaid_access_tokens:
        console.print(
            "[bold red]No PLAID_ACCESS_TOKENS configured.[/bold red]\n"
            "Run [bold]portfolio-bot link[/bold] to connect a Fidelity account."
        )
        sys.exit(1)

    with console.status("[bold green]Fetching portfolio data from Plaid…"):
        snapshot = fetcher.fetch_snapshot(txn_days=txn_days)

    snapshot_id = store.save_snapshot(snapshot)

    table = Table(title="Portfolio Snapshot", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Snapshot ID", str(snapshot_id))
    table.add_row("Fetched At", snapshot.fetched_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    table.add_row("Accounts", str(len(snapshot.accounts)))
    table.add_row("Holdings", str(len(snapshot.holdings)))
    table.add_row("Transactions", str(len(snapshot.transactions)))
    table.add_row("Total Value", f"${snapshot.total_value:,.2f}")
    console.print(table)


# ── digest ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--send/--no-send", default=False, help="Send alerts (Slack/email) after analysis.")
@click.option("--show-thinking/--no-show-thinking", default=False, help="Print Claude's thinking.")
def digest(send: bool, show_thinking: bool):
    """Run the daily portfolio digest using Claude AI."""
    from src.analyzer import PortfolioAnalyzer
    from src.alerter import Alerter

    cfg, store, _, _ = _make_deps()
    analyzer = PortfolioAnalyzer(cfg, store)

    with console.status("[bold green]Analysing portfolio with Claude…"):
        result = analyzer.daily_digest()

    if show_thinking and result.thinking:
        console.print(
            Panel(result.thinking, title="[dim]Claude Thinking[/dim]", border_style="dim")
        )

    console.print(Markdown(result.content))
    console.print(
        f"\n[dim]Tokens — input: {result.input_tokens:,}  output: {result.output_tokens:,}[/dim]"
    )

    if send:
        alerter = Alerter(cfg, store)
        delivered = alerter.send_daily_digest(result.content)
        console.print(f"[green]Alert delivered via: {delivered}[/green]")


# ── ask ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--show-thinking/--no-show-thinking", default=False)
def ask(question: tuple[str, ...], show_thinking: bool):
    """Ask Claude a question about your portfolio.

    Example: portfolio-bot ask "What is my largest position?"
    """
    from src.analyzer import PortfolioAnalyzer

    cfg, store, _, _ = _make_deps()
    analyzer = PortfolioAnalyzer(cfg, store)
    q = " ".join(question)

    console.print(f"[bold]Q:[/bold] {q}\n")

    with console.status("[bold green]Thinking…"):
        result = analyzer.answer_query(q)

    if show_thinking and result.thinking:
        console.print(
            Panel(result.thinking, title="[dim]Claude Thinking[/dim]", border_style="dim")
        )

    console.print(Markdown(result.content))
    console.print(
        f"\n[dim]Tokens — input: {result.input_tokens:,}  output: {result.output_tokens:,}[/dim]"
    )


# ── check-alerts ──────────────────────────────────────────────────────────────

@cli.command("check-alerts")
@click.option("--send/--no-send", default=False, help="Deliver alerts if thresholds breached.")
def check_alerts(send: bool):
    """Check drift and daily-loss thresholds and optionally send alerts."""
    from src.analyzer import PortfolioAnalyzer
    from src.alerter import Alerter

    cfg, store, _, _ = _make_deps()
    analyzer = PortfolioAnalyzer(cfg, store)
    alerter = Alerter(cfg, store)

    # Drift check
    with console.status("[bold yellow]Checking portfolio drift…"):
        drift = analyzer.check_drift()

    if drift is None:
        console.print("[dim]Not enough snapshots for drift analysis.[/dim]")
    elif drift.alert_required:
        console.print(
            Panel(
                Markdown(f"**{drift.summary}**\n\n" + drift.raw_content),
                title="⚠️  Drift Alert",
                border_style="yellow",
            )
        )
        if send:
            alerter.send_drift_alert(drift.summary, drift.drifted_positions)
    else:
        console.print(f"[green]✓ Drift within threshold:[/green] {drift.summary}")

    # Daily loss check
    loss_triggered, loss_msg = analyzer.check_daily_loss()
    if loss_triggered:
        console.print(Panel(loss_msg, title="🔴  Daily Loss Alert", border_style="red"))
        if send:
            alerter.send_loss_alert(loss_msg)
    else:
        console.print(f"[green]✓ Daily loss within threshold:[/green] {loss_msg}")


# ── holdings ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--sort", type=click.Choice(["value", "gain", "ticker"]), default="value")
@click.option("--limit", default=20, show_default=True, help="Max rows to display.")
def holdings(sort: str, limit: int):
    """Display current holdings from the latest local snapshot."""
    _, store, _, _ = _make_deps()
    payload = store.get_latest_snapshot_payload()

    if not payload:
        console.print("[red]No snapshot found. Run `portfolio-bot fetch` first.[/red]")
        sys.exit(1)

    hs = payload.get("holdings", [])

    sort_key = {
        "value": lambda h: float(h.get("institution_value") or 0),
        "gain": lambda h: float(h.get("unrealised_gain") or 0),
        "ticker": lambda h: h.get("ticker") or "",
    }[sort]
    hs_sorted = sorted(hs, key=sort_key, reverse=(sort != "ticker"))[:limit]

    table = Table(
        title=f"Holdings — {payload.get('fetched_at', 'unknown')}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Ticker", style="bold")
    table.add_column("Name", max_width=30, no_wrap=True)
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Gain/Loss", justify="right")
    table.add_column("Gain %", justify="right")

    total_value = float(payload.get("total_value") or 0)

    for h in hs_sorted:
        val = float(h.get("institution_value") or 0)
        gain = h.get("unrealised_gain")
        gain_pct = h.get("unrealised_gain_pct")
        pct_of_portfolio = val / total_value * 100 if total_value else 0

        gain_str = f"${float(gain):+,.2f}" if gain is not None else "–"
        gain_pct_str = f"{float(gain_pct):+.2f}%" if gain_pct is not None else "–"
        gain_style = "green" if gain and float(gain) >= 0 else "red"

        table.add_row(
            h.get("ticker") or "?",
            h.get("name") or "?",
            f"{float(h.get('quantity') or 0):,.4f}",
            f"${float(h.get('institution_price') or 0):,.2f}",
            f"${val:,.2f} ({pct_of_portfolio:.1f}%)",
            f"[{gain_style}]{gain_str}[/{gain_style}]",
            f"[{gain_style}]{gain_pct_str}[/{gain_style}]",
        )

    console.print(table)
    console.print(f"\n[bold]Total Portfolio Value:[/bold] ${total_value:,.2f}")


# ── link ──────────────────────────────────────────────────────────────────────

@cli.command()
def link():
    """Generate a Plaid Link token for connecting a new brokerage account."""
    cfg, _, plaid, _ = _make_deps()

    with console.status("[bold green]Creating Plaid Link token…"):
        token = plaid.create_link_token()

    console.print(
        Panel(
            f"[bold]Link Token:[/bold]\n{token}\n\n"
            "Use this token with the Plaid Link SDK to connect your Fidelity account.\n"
            "After linking, store the resulting access_token in PLAID_ACCESS_TOKENS.",
            title="Plaid Link",
            border_style="blue",
        )
    )


# ── research ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--max-tickers", default=5, show_default=True,
              help="Number of top positions to research in depth.")
@click.option("--send/--no-send", default=False, help="Send research report via Slack/email.")
@click.option("--show-thinking/--no-show-thinking", default=False,
              help="Print Claude's thinking for each research phase.")
def research(max_tickers: int, send: bool, show_thinking: bool):
    """Run Karpathy-style autonomous research on your top positions.

    Runs a three-phase loop:
      1. Plan  — Claude builds a prioritised research agenda
      2. Research — deep per-ticker + macro analysis
      3. Synthesise — integrates findings into gains-focused signals
    """
    from src.auto_researcher import AutoResearcher
    from src.analyzer import PortfolioAnalyzer
    from src.alerter import Alerter

    cfg, store, _, _ = _make_deps()
    researcher = AutoResearcher(cfg, store)

    with console.status(f"[bold green]Running auto-research ({max_tickers} tickers)…"):
        report = researcher.conduct_research(max_tickers=max_tickers)

    # Show per-ticker thinking if requested
    if show_thinking:
        for ticker, r in report.ticker_research.items():
            if r.thinking:
                console.print(
                    Panel(r.thinking, title=f"[dim]Thinking: {ticker}[/dim]", border_style="dim")
                )

    report_md = researcher.format_report_markdown(report)
    console.print(Panel(Markdown(report_md), title="Research Report", border_style="cyan"))

    # Show optimisation alongside research
    console.print("\n[bold]Generating gains-optimised allocation…[/bold]")
    with console.status("[bold green]Optimising portfolio…"):
        from src.optimizer import PortfolioOptimizer
        optimizer = PortfolioOptimizer(store)
        opt_result = optimizer.optimize(research_report=report)

    opt_md = optimizer.format_result_markdown(opt_result)
    console.print(Panel(Markdown(opt_md), title="Gains Optimisation", border_style="green"))

    # Synthesise into a research digest via Claude
    console.print("\n[bold]Synthesising research digest…[/bold]")
    analyzer = PortfolioAnalyzer(cfg, store)
    with console.status("[bold green]Generating research digest…"):
        digest = analyzer.research_digest(report_md + "\n\n" + opt_md)

    console.print(Panel(Markdown(digest.content), title="Research-Backed Digest", border_style="magenta"))
    console.print(
        f"\n[dim]Research tokens — input: {report.total_input_tokens:,}  "
        f"output: {report.total_output_tokens:,}\n"
        f"Digest tokens — input: {digest.input_tokens:,}  output: {digest.output_tokens:,}[/dim]"
    )

    if send:
        alerter = Alerter(cfg, store)
        delivered = alerter.send_daily_digest(digest.content)
        console.print(f"[green]Research digest sent via: {delivered}[/green]")


# ── optimize ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--max-position", default=0.30, show_default=True,
              help="Max allocation for any single position (0.30 = 30%).")
@click.option("--kelly-fraction", default=0.50, show_default=True,
              help="Partial-Kelly scaling factor (0.5 = half Kelly).")
@click.option("--with-research/--no-research", default=False,
              help="Run auto-research first to overlay signals on optimisation.")
@click.option("--max-tickers", default=5, show_default=True,
              help="Tickers to research when --with-research is set.")
def optimize(max_position: float, kelly_fraction: float, with_research: bool, max_tickers: int):
    """Compute gains-maximising portfolio weights using Kelly criterion + momentum.

    Objective: maximise expected portfolio gains subject to concentration limits.
    Uses historical snapshot data for return/volatility estimates.  Pass
    --with-research to layer in Claude's fundamental analysis signals.
    """
    from src.optimizer import PortfolioOptimizer

    cfg, store, _, _ = _make_deps()

    research_report = None
    if with_research:
        from src.auto_researcher import AutoResearcher
        with console.status(f"[bold green]Running auto-research ({max_tickers} tickers)…"):
            researcher = AutoResearcher(cfg, store)
            research_report = researcher.conduct_research(max_tickers=max_tickers)
        console.print(
            f"[dim]Research done — {len(research_report.tickers_researched)} tickers, "
            f"{research_report.total_input_tokens:,} input tokens[/dim]"
        )

    optimizer = PortfolioOptimizer(
        store,
        max_position_pct=max_position,
        kelly_fraction=kelly_fraction,
    )

    with console.status("[bold green]Optimising portfolio…"):
        result = optimizer.optimize(research_report=research_report)

    console.print(Markdown(optimizer.format_result_markdown(result)))

    if result.data_quality_note:
        console.print(f"\n[yellow]Note:[/yellow] {result.data_quality_note}")


# ── alerts-log ────────────────────────────────────────────────────────────────

@cli.command("alerts-log")
@click.option("--limit", default=10, show_default=True)
def alerts_log(limit: int):
    """Show recent alerts from the log."""
    _, store, _, _ = _make_deps()
    rows = store.get_recent_alerts(limit)

    if not rows:
        console.print("[dim]No alerts logged yet.[/dim]")
        return

    table = Table(title="Recent Alerts", show_header=True, header_style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Type")
    table.add_column("Ticker")
    table.add_column("Created At")
    table.add_column("Delivered")
    table.add_column("Message", max_width=60, no_wrap=True)

    for r in rows:
        delivered_str = str(r.get("delivered", 0))
        table.add_row(
            str(r["id"]),
            r.get("alert_type") or "",
            r.get("ticker") or "–",
            r.get("created_at") or "",
            delivered_str,
            (r.get("message") or "")[:60],
        )

    console.print(table)
