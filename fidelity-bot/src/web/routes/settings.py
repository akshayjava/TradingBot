"""Settings — edit .env config, test alert channels, trigger manual jobs."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()

_ENV_FILE = Path(__file__).parent.parent.parent.parent / ".env"

_EDITABLE_KEYS = [
    # (env_key, label, secret, placeholder)
    ("PLAID_CLIENT_ID",           "Plaid Client ID",            False, ""),
    ("PLAID_SECRET",              "Plaid Secret",               True,  ""),
    ("PLAID_ENV",                 "Plaid Environment",          False, "sandbox|development|production"),
    ("ANTHROPIC_API_KEY",         "Anthropic API Key",          True,  "sk-ant-…"),
    ("SLACK_BOT_TOKEN",           "Slack Bot Token",            True,  "xoxb-…  (optional)"),
    ("SLACK_CHANNEL",             "Slack Channel",              False, "#portfolio-alerts"),
    ("RESEND_API_KEY",            "Resend API Key",             True,  "re_…  (optional)"),
    ("EMAIL_FROM",                "Email From",                 False, "bot@yourdomain.com"),
    ("EMAIL_TO",                  "Email To",                   False, "you@yourdomain.com"),
    ("ALERT_DRIFT_THRESHOLD",     "Drift Alert Threshold",      False, "0.05"),
    ("ALERT_DAILY_LOSS_THRESHOLD","Daily Loss Threshold",       False, "0.02"),
    ("SCHEDULER_CRON_HOUR",       "Scheduler Hour (ET)",        False, "18"),
    ("SCHEDULER_CRON_MINUTE",     "Scheduler Minute",           False, "0"),
    ("RESEARCH_ENABLED",          "Research Enabled",           False, "true|false"),
    ("RESEARCH_MAX_TICKERS",      "Research Max Tickers",       False, "5"),
]


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def _write_env_file(updates: dict[str, str]) -> None:
    current = _read_env_file()
    current.update({k: v for k, v in updates.items() if v != ""})

    lines = []
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in current:
                    lines.append(f"{k}={current.pop(k)}")
                    continue
            lines.append(line)
    # Append any new keys
    for k, v in current.items():
        lines.append(f"{k}={v}")

    _ENV_FILE.write_text("\n".join(lines) + "\n")

    # Reload os.environ and config singleton
    for k, v in updates.items():
        if v:
            os.environ[k] = v

    import src.config as _cfg_mod
    _cfg_mod._config = None


@router.get("/settings")
async def settings_page(request: Request):
    current = _read_env_file()
    fields = []
    for key, label, secret, placeholder in _EDITABLE_KEYS:
        val = current.get(key, os.environ.get(key, ""))
        fields.append({
            "key": key,
            "label": label,
            "secret": secret,
            "placeholder": placeholder,
            "value": val,
        })
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active": "settings",
        "fields": fields,
    })


@router.post("/api/settings")
async def save_settings(request: Request):
    form = await request.form()
    updates = {k: str(v).strip() for k, v in form.items() if str(v).strip()}
    try:
        _write_env_file(updates)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/settings/test-slack")
async def test_slack():
    try:
        from src.alerter import Alerter
        from src.config import get_config
        from src.data_store import DataStore
        cfg = get_config()
        if not cfg.slack_enabled:
            return JSONResponse({"error": "Slack not configured"}, status_code=400)
        store = DataStore(cfg.db_path, cfg.snapshots_dir)
        alerter = Alerter(cfg, store)
        alerter._send_slack("🤖 Portfolio Bot test message — Slack is working!")
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/settings/test-email")
async def test_email():
    try:
        from src.alerter import Alerter
        from src.config import get_config
        from src.data_store import DataStore
        cfg = get_config()
        if not cfg.email_enabled:
            return JSONResponse({"error": "Email not configured"}, status_code=400)
        store = DataStore(cfg.db_path, cfg.snapshots_dir)
        alerter = Alerter(cfg, store)
        alerter._send_email("Portfolio Bot Test", "Email delivery is working!")
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/settings/run-digest")
async def manual_digest():
    """Trigger the full daily job immediately."""
    import threading
    def _run():
        from scheduler import run_daily_job
        run_daily_job()
    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "Daily job started in background"})
