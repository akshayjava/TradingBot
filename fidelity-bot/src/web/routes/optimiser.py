"""Optimiser — gains-maximising portfolio allocation."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()


def _get_deps():
    from src.config import get_config
    from src.data_store import DataStore
    cfg = get_config()
    store = DataStore(cfg.db_path, cfg.snapshots_dir)
    return cfg, store


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("/optimize")
async def optimiser_page(request: Request):
    return templates.TemplateResponse("optimiser.html", {
        "request": request,
        "active": "optimize",
    })


# ── API ───────────────────────────────────────────────────────────────────────

@router.post("/api/optimize")
async def run_optimiser(
    max_position: float = 0.30,
    kelly_fraction: float = 0.50,
    use_research: bool = False,
):
    """Run the gains-maximising optimiser and return the result as JSON."""
    cfg, store = _get_deps()

    snapshot = store.get_latest_snapshot_payload()
    if not snapshot:
        return JSONResponse({"error": "No snapshot found. Fetch your portfolio first."}, status_code=400)

    research_report = None
    if use_research:
        latest = store.get_latest_research_report()
        if latest:
            # Reconstruct a minimal object the optimizer can consume
            # (just needs ticker_final_signals)
            import json as _json
            from src.auto_researcher import ResearchReport, MacroResearch
            rj = _json.loads(latest["report_json"]) if isinstance(latest["report_json"], str) else latest["report_json"]
            research_report = ResearchReport(
                generated_at=latest["generated_at"],
                tickers_researched=latest["tickers_researched"],
                ticker_research={},
                macro_research=MacroResearch([], [], [], []),
                top_opportunities=rj.get("top_opportunities", []),
                top_risks=rj.get("top_risks", []),
                ticker_final_signals=rj.get("ticker_final_signals", []),
                rebalancing_actions=[],
                executive_summary=rj.get("executive_summary", ""),
            )

    from src.optimizer import PortfolioOptimizer
    optimizer = PortfolioOptimizer(
        store,
        max_position_pct=max_position,
        kelly_fraction=kelly_fraction,
    )
    result = optimizer.optimize(research_report=research_report)

    # Serialise result to JSON-safe dict
    actions = [
        {
            "ticker": a.ticker,
            "name": a.name,
            "current_pct": a.current_pct,
            "target_pct": a.target_pct,
            "delta_pct": a.delta_pct,
            "delta_dollars": a.delta_dollars,
            "action": a.action,
            "priority": a.priority,
        }
        for a in result.rebalancing_actions
    ]
    stats = [
        {
            "ticker": t,
            "name": s.name,
            "current_pct": round(s.current_weight * 100, 2),
            "target_pct": round(result.target_weights.get(t, 0) * 100, 2),
            "mean_return": round(s.mean_return * 100, 4),
            "volatility": round(s.volatility * 100, 4),
            "momentum": round(s.momentum_score, 3),
            "kelly_pct": round(s.kelly_weight * 100, 2),
            "signal": s.research_signal,
            "confidence": round(s.research_confidence, 2),
            "adj_return": round(s.adjusted_expected_return * 100, 4),
        }
        for t, s in sorted(
            result.ticker_stats.items(),
            key=lambda kv: kv[1].adjusted_expected_return,
            reverse=True,
        )
    ]

    return JSONResponse({
        "total_value": result.total_portfolio_value,
        "expected_gain_pct": round(result.expected_portfolio_gain * 100, 4),
        "actions": actions,
        "stats": stats,
        "data_quality_note": result.data_quality_note,
    })
