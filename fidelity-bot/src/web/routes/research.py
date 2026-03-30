"""Research — Karpathy-style autonomous research loop with SSE streaming."""

from __future__ import annotations

import json
import queue as queue_module
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()

# Active research jobs: run_id → threading.Queue
_research_jobs: dict[str, queue_module.Queue] = {}


def _get_deps():
    from src.config import get_config
    from src.data_store import DataStore
    cfg = get_config()
    store = DataStore(cfg.db_path, cfg.snapshots_dir)
    return cfg, store


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("/research")
async def research_page(request: Request):
    _, store = _get_deps()
    latest = store.get_latest_research_report()
    return templates.TemplateResponse("research.html", {
        "request": request,
        "active": "research",
        "latest_report": latest,
    })


# ── API ───────────────────────────────────────────────────────────────────────

@router.post("/api/research")
async def start_research(max_tickers: int = 5):
    """Start a background research run. Returns run_id for SSE stream."""
    cfg, store = _get_deps()

    snapshot = store.get_latest_snapshot_payload()
    if not snapshot:
        return JSONResponse({"error": "No snapshot found. Fetch your portfolio first."}, status_code=400)

    run_id = str(uuid.uuid4())
    q: queue_module.Queue = queue_module.Queue()
    _research_jobs[run_id] = q

    def _run():
        try:
            from src.auto_researcher import AutoResearcher

            def on_phase(phase: str):
                q.put({"type": "phase", "phase": phase})

            def on_token(text: str):
                q.put({"type": "token", "text": text})

            researcher = AutoResearcher(cfg, store)
            report = researcher.conduct_research(
                max_tickers=max_tickers,
                on_phase=on_phase,
                on_token=on_token,
            )

            report_md = researcher.format_report_markdown(report)

            # Persist to DB
            store.save_research_report(
                generated_at=report.generated_at,
                tickers=report.tickers_researched,
                report_json={
                    "top_opportunities": report.top_opportunities,
                    "top_risks": report.top_risks,
                    "rebalancing_actions": [
                        {
                            "rank": a.rank,
                            "ticker": a.ticker,
                            "action": a.action,
                            "rationale": a.rationale,
                            "suggested_delta_pct": a.suggested_delta_pct,
                        }
                        for a in report.rebalancing_actions
                    ],
                    "ticker_final_signals": report.ticker_final_signals,
                    "executive_summary": report.executive_summary,
                },
                report_md=report_md,
                total_input_tokens=report.total_input_tokens,
                total_output_tokens=report.total_output_tokens,
            )

            q.put({
                "type": "done",
                "report_md": report_md,
                "tickers": report.tickers_researched,
                "input_tokens": report.total_input_tokens,
                "output_tokens": report.total_output_tokens,
            })

        except Exception as exc:
            q.put({"type": "error", "msg": str(exc)})

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"run_id": run_id})


@router.get("/api/research/stream/{run_id}")
async def research_stream(run_id: str):
    """SSE stream of research progress: phase changes and token-by-token output."""
    import asyncio

    q = _research_jobs.get(run_id)
    if not q:
        return JSONResponse({"error": "run not found"}, status_code=404)

    async def generate():
        while True:
            try:
                event = q.get_nowait()
            except queue_module.Empty:
                await asyncio.sleep(0.04)
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                _research_jobs.pop(run_id, None)
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/research/latest")
async def latest_research():
    _, store = _get_deps()
    report = store.get_latest_research_report()
    if not report:
        return JSONResponse({"error": "No research report found"}, status_code=404)
    return JSONResponse(report)
