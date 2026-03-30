"""Dashboard — portfolio overview, fetch trigger, holdings table."""

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

# Active fetch jobs: run_id → threading.Queue
_fetch_jobs: dict[str, queue_module.Queue] = {}


def _get_deps():
    from src.config import get_config
    from src.data_store import DataStore
    cfg = get_config()
    store = DataStore(cfg.db_path, cfg.snapshots_dir)
    return cfg, store


def _compute_day_change(snapshots: list[dict]) -> dict:
    if len(snapshots) < 2:
        return {"dollar": 0.0, "pct": 0.0}
    curr = float(snapshots[0].get("total_value") or 0)
    prev = float(snapshots[1].get("total_value") or 0)
    dollar = curr - prev
    pct = (dollar / prev * 100) if prev else 0.0
    return {"dollar": dollar, "pct": pct}


def _enrich_holdings(holdings: list[dict], total_value: float) -> list[dict]:
    for h in holdings:
        val = float(h.get("institution_value") or 0)
        h["pct"] = round(val / total_value * 100, 2) if total_value else 0.0
    return sorted(holdings, key=lambda h: float(h.get("institution_value") or 0), reverse=True)


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("/")
async def dashboard(request: Request):
    _, store = _get_deps()
    snapshots = store.get_snapshot_payloads(limit=2)
    snapshot = snapshots[0] if snapshots else {}
    total_value = float(snapshot.get("total_value") or 0)
    holdings = _enrich_holdings(snapshot.get("holdings", []), total_value)
    day_change = _compute_day_change(snapshots)
    accounts = snapshot.get("accounts", [])
    fetched_at = snapshot.get("fetched_at", "Never")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active": "dashboard",
        "snapshot": snapshot,
        "holdings": holdings,
        "total_value": total_value,
        "day_change": day_change,
        "accounts": accounts,
        "fetched_at": fetched_at,
        "has_data": bool(snapshots),
    })


# ── API ───────────────────────────────────────────────────────────────────────

@router.get("/api/snapshot")
async def api_snapshot():
    _, store = _get_deps()
    payload = store.get_latest_snapshot_payload()
    if not payload:
        return JSONResponse({"error": "No snapshot found"}, status_code=404)
    return JSONResponse(payload)


@router.post("/api/fetch")
async def api_fetch(txn_days: int = 30):
    """Start a background portfolio fetch. Returns run_id for SSE stream."""
    cfg, store = _get_deps()
    if not cfg.plaid_access_tokens:
        return JSONResponse({"error": "No Plaid access tokens configured"}, status_code=400)

    run_id = str(uuid.uuid4())
    q: queue_module.Queue = queue_module.Queue()
    _fetch_jobs[run_id] = q

    def _run():
        try:
            from src.data_fetcher import DataFetcher
            from src.plaid_client import PlaidClient
            q.put({"type": "status", "msg": "Connecting to Plaid…"})
            plaid = PlaidClient(cfg)
            fetcher = DataFetcher(plaid)
            q.put({"type": "status", "msg": "Fetching holdings and transactions…"})
            snapshot = fetcher.fetch_snapshot(txn_days=txn_days)
            snapshot_id = store.save_snapshot(snapshot)
            q.put({
                "type": "done",
                "snapshot_id": snapshot_id,
                "total_value": str(snapshot.total_value),
                "holdings": len(snapshot.holdings),
                "transactions": len(snapshot.transactions),
            })
        except Exception as exc:
            q.put({"type": "error", "msg": str(exc)})

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"run_id": run_id})


@router.get("/api/fetch/stream/{run_id}")
async def fetch_stream(run_id: str):
    """SSE stream for a running fetch job."""
    import asyncio

    q = _fetch_jobs.get(run_id)
    if not q:
        return JSONResponse({"error": "run not found"}, status_code=404)

    async def generate():
        while True:
            try:
                event = q.get_nowait()
            except queue_module.Empty:
                await asyncio.sleep(0.1)
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                _fetch_jobs.pop(run_id, None)
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
