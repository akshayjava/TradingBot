"""Alerts log — history of all drift, loss, and digest alerts."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()


def _get_store():
    from src.config import get_config
    from src.data_store import DataStore
    cfg = get_config()
    return DataStore(cfg.db_path, cfg.snapshots_dir)


_TYPE_LABELS = {
    "daily_digest": ("Digest", "secondary"),
    "drift_alert":  ("Drift",  "warning"),
    "daily_loss":   ("Loss",   "danger"),
}


@router.get("/alerts")
async def alerts_page(request: Request, limit: int = 50, alert_type: str = ""):
    store = _get_store()
    rows = store.get_recent_alerts(limit=limit)

    if alert_type:
        rows = [r for r in rows if r.get("alert_type") == alert_type]

    for r in rows:
        at = r.get("alert_type", "")
        r["label"], r["badge_class"] = _TYPE_LABELS.get(at, (at, ""))
        delivered = int(r.get("delivered") or 0)
        r["slack"] = bool(delivered & 1)
        r["email"] = bool(delivered & 2)

    return templates.TemplateResponse("alerts.html", {
        "request": request,
        "active": "alerts",
        "alerts": rows,
        "filter_type": alert_type,
    })


@router.get("/api/alerts")
async def api_alerts(limit: int = 50):
    store = _get_store()
    return JSONResponse(store.get_recent_alerts(limit=limit))
