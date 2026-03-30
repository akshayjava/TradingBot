"""Setup wizard — first-time configuration and Plaid account linking."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()

_ENV_FILE = Path(__file__).parent.parent.parent.parent / ".env"


def _write_env_values(updates: dict[str, str]) -> None:
    """Append / overwrite key=value pairs in .env and os.environ."""
    existing: dict[str, str] = {}
    lines: list[str] = []

    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                existing[k] = stripped.partition("=")[2]
                lines.append(line)
            else:
                lines.append(line)
    else:
        _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Update or append
    for k, v in updates.items():
        if not v:
            continue
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                lk = stripped.partition("=")[0].strip()
                if lk == k:
                    lines[i] = f"{k}={v}"
                    replaced = True
                    break
        if not replaced:
            lines.append(f"{k}={v}")
        os.environ[k] = v

    _ENV_FILE.write_text("\n".join(lines) + "\n")

    # Reset config singleton
    try:
        import src.config as _cfg_mod
        _cfg_mod._config = None
    except Exception:
        pass


@router.get("/setup")
async def setup_page(request: Request):
    # Detect how far along setup is
    step = 1
    try:
        from src.config import get_config
        cfg = get_config()
        step = 2 if not cfg.plaid_access_tokens else 3
    except Exception:
        step = 1

    return templates.TemplateResponse("setup.html", {
        "request": request,
        "active": "setup",
        "step": step,
    })


@router.post("/api/setup/keys")
async def save_keys(request: Request):
    form = await request.form()
    updates = {k: str(v).strip() for k, v in form.items() if str(v).strip()}
    required = ["PLAID_CLIENT_ID", "PLAID_SECRET", "ANTHROPIC_API_KEY"]
    missing = [k for k in required if not updates.get(k)]
    if missing:
        return JSONResponse({"error": f"Missing required fields: {', '.join(missing)}"}, status_code=400)
    try:
        _write_env_values(updates)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/setup/plaid-link-token")
async def create_link_token():
    try:
        from src.config import get_config
        from src.plaid_client import PlaidClient
        cfg = get_config()
        client = PlaidClient(cfg)
        token = client.create_link_token()
        return JSONResponse({"link_token": token})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/setup/plaid-exchange")
async def exchange_token(request: Request):
    body = await request.json()
    public_token = body.get("public_token", "")
    if not public_token:
        return JSONResponse({"error": "public_token is required"}, status_code=400)
    try:
        from src.config import get_config
        from src.plaid_client import PlaidClient
        cfg = get_config()
        client = PlaidClient(cfg)
        access_token = client.exchange_public_token(public_token)

        # Append the new token to existing ones
        existing = cfg.plaid_access_tokens
        all_tokens = existing + [access_token]
        _write_env_values({"PLAID_ACCESS_TOKENS": ",".join(all_tokens)})

        return JSONResponse({"ok": True, "access_token_suffix": access_token[-6:]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
