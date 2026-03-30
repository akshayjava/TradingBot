"""FastAPI web application for the Portfolio Intelligence Bot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.web.routes import alerts, dashboard, optimiser, research, settings, setup

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# Custom Jinja2 filters
def _format_number(value) -> str:
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)

templates.env.filters["format_number"] = _format_number


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data directory exists before any request is handled
    try:
        from src.config import get_config
        cfg = get_config()
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    except EnvironmentError:
        pass  # Config not yet set up — setup wizard will handle it
    yield


app = FastAPI(title="Portfolio Intelligence Bot", lifespan=lifespan)

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(dashboard.router)
app.include_router(research.router)
app.include_router(optimiser.router)
app.include_router(alerts.router)
app.include_router(settings.router)
app.include_router(setup.router)


# ── Config-check middleware: redirect to setup if not configured ──────────────
@app.middleware("http")
async def require_setup(request: Request, call_next):
    exempt = {"/setup", "/api/setup", "/static", "/favicon.ico"}
    path = request.url.path
    if any(path.startswith(e) for e in exempt):
        return await call_next(request)

    try:
        from src.config import get_config
        get_config()
    except (EnvironmentError, Exception):
        return RedirectResponse("/setup")

    return await call_next(request)


# ── Entry point ───────────────────────────────────────────────────────────────
def serve(host: str = "0.0.0.0", port: int = 8080, reload: bool = False) -> None:
    """Start the web server.  Called by the `portfolio-web` console script."""
    uvicorn.run("src.web:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    serve(reload=True)
