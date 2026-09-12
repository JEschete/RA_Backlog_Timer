"""FastAPI application serving the backlog dashboard."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import credentials as creds_module
from ..config import DB_FILE, DEFAULT_EXPORT, log, resolve_db_path
from ..credentials import Credentials
from ..efficiency import add_metrics, plan_session, summarize
from .. import calibration as calib_module
from .. import preferences
from ..scanner import ScanOptions
from ..storage import annotations
from ..storage import export, migrate, repo
from ..storage.db import connect
from .scanjobs import manager
from . import routes_features

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="RA Backlog Timer")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# Set by serve(); lets the CLI point the server at a non-default database.
_db_path: str = str(resolve_db_path())


def configure(db_path: str | None = None, rom_roots=None) -> None:
    global _db_path
    _db_path = str(resolve_db_path(db_path))
    routes_features.set_launch_roots(rom_roots or [])


def get_conn() -> sqlite3.Connection:
    conn = connect(_db_path)
    try:
        yield conn
    finally:
        conn.close()


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# --- data -------------------------------------------------------------------

def _clean(value):
    """JSON-safe value: NaN/NaT become None, numpy scalars become Python ones."""
    if value is None:
        return None
    # Containers first: pd.isna() on a list returns an elementwise array, and
    # `if` on that raises. The tags column holds lists.
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        return None if pd.isna(value) else round(float(value), 2)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass          # not a scalar pandas understands; pass it through
    return value


def _frame_to_records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


@app.get("/api/games")
async def api_games(conn: sqlite3.Connection = Depends(get_conn)):
    df = routes_features._enriched(conn)
    return {"games": _frame_to_records(df)}


@app.get("/api/summary")
async def api_summary(conn: sqlite3.Connection = Depends(get_conn)):
    df = routes_features._enriched(conn)
    stats = summarize(df) if not df.empty else {
        "total_games": 0, "with_hltb": 0, "with_ra_mastery": 0,
        "total_hours": 0.0, "avg_hours": 0.0, "total_points": 0, "no_time_data": 0,
    }
    stats["systems"] = [{"name": n, "count": c} for n, c in repo.list_systems(conn)]
    stats["username"] = repo.get_meta(conn, "username")
    stats["cache"] = repo.cache_stats(conn)
    stats["db_path"] = _db_path        # surfaced so "which backlog is this?" is answerable
    stats["preferences"] = preferences.load(conn).to_dict()
    cal = routes_features._calibration(conn)
    stats["calibration"] = {"factor": cal.factor, "confident": cal.confident,
                            "description": cal.description, "samples": cal.samples}
    velocity = calib_module.velocity(annotations.sessions_for(conn))
    stats["velocity"] = velocity
    stats["projection"] = calib_module.project_completion(
        stats.get("total_hours", 0.0), velocity.get("hours_per_week", 0.0))
    if not df.empty and "match_quality" in df.columns:
        counts = df["match_quality"].value_counts(dropna=True).to_dict()
        stats["match_quality"] = {str(k): int(v) for k, v in counts.items()}
    else:
        stats["match_quality"] = {}
    return stats


@app.get("/api/plan")
async def api_plan(budget: float = 20.0, max_games: int | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)):
    """Item #20: best points-per-hour set of games within an hour budget."""
    if budget <= 0:
        raise HTTPException(400, "budget must be positive")
    df = repo.load_games(conn)
    plan = plan_session(df, budget_hours=budget, max_games=max_games)
    return {
        "games": [asdict(g) for g in plan.games],
        "total_hours": plan.total_hours,
        "total_points": plan.total_points,
        "budget_hours": plan.budget_hours,
        "efficiency": plan.efficiency,
    }


# --- credentials ------------------------------------------------------------

@app.get("/api/credentials")
async def api_credentials_status():
    creds = creds_module.load()
    return {
        "configured": creds is not None and creds.is_complete(),
        "username": creds.username if creds else None,
        "storage": creds_module.storage_description(),
    }


@app.post("/api/credentials")
async def api_credentials_save(payload: dict):
    username = (payload.get("username") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if not username or not api_key:
        raise HTTPException(400, "username and api_key are both required")
    creds_module.save(Credentials(username, api_key))
    return {"ok": True, "username": username}


@app.delete("/api/credentials")
async def api_credentials_clear():
    creds_module.clear()
    return {"ok": True}


# --- scanning ---------------------------------------------------------------

@app.get("/api/scan/status")
async def api_scan_status():
    return manager.status()


@app.post("/api/scan")
async def api_scan_start(payload: dict | None = None):
    payload = payload or {}
    creds = creds_module.load()
    if creds is None or not creds.is_complete():
        raise HTTPException(400, "No credentials stored")
    if manager.running:
        raise HTTPException(409, "A scan is already running")

    options = ScanOptions(
        fresh=bool(payload.get("fresh")),
        systems=payload.get("systems") or None,
        exclude_systems=payload.get("exclude_systems") or None,
        fetch_user_progress=payload.get("user_progress", True),
    )
    # The scan owns its connection for its whole lifetime.
    conn = connect(_db_path)
    if not manager.start(conn, creds, options):
        conn.close()
        raise HTTPException(409, "A scan is already running")
    return {"ok": True}


@app.post("/api/scan/cancel")
async def api_scan_cancel():
    return {"ok": await manager.cancel()}


@app.get("/api/scan/stream")
async def api_scan_stream():
    """Server-sent events. One-directional, so no WebSocket needed."""

    async def event_stream():
        async with manager.subscribe() as queue:
            while True:
                try:
                    progress = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(progress.to_event())}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"      # keeps proxies from closing us
                except asyncio.CancelledError:
                    break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- export -----------------------------------------------------------------

@app.post("/api/export/{fmt}")
async def api_export(fmt: str, conn: sqlite3.Connection = Depends(get_conn)):
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(400, "format must be xlsx or csv")
    target = Path(DEFAULT_EXPORT)
    try:
        path = export.to_excel(conn, target) if fmt == "xlsx" else export.to_csv(conn, target)
    except PermissionError:
        # Item #6: say so plainly instead of dying mid-write.
        raise HTTPException(
            409, f"{target} is open in another program. Close it and try again.")
    return {"ok": True, "path": str(path.resolve()), "name": path.name}


@app.get("/api/export/{fmt}/download")
async def api_export_download(fmt: str, conn: sqlite3.Connection = Depends(get_conn)):
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(400, "format must be xlsx or csv")
    target = Path(DEFAULT_EXPORT)
    try:
        path = export.to_excel(conn, target) if fmt == "xlsx" else export.to_csv(conn, target)
    except PermissionError:
        raise HTTPException(409, f"{target} is open in another program.")
    return FileResponse(path, filename=path.name)


app.include_router(routes_features._routes(get_conn))


# --- startup ----------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    conn = connect(_db_path)
    try:
        if migrate.needs_migration(conn):
            log.info("Importing data from the pre-SQLite files...")
            migrate.run(conn)
    finally:
        conn.close()


def serve(host: str, port: int, db_path: str | None = None,
          open_browser: bool = True, rom_roots=None) -> None:
    import uvicorn

    configure(db_path, rom_roots)
    url = f"http://{host}:{port}"
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    log.info("Serving the dashboard at %s  (Ctrl+C to stop)", url)
    uvicorn.run(app, host=host, port=port, log_level="warning")
