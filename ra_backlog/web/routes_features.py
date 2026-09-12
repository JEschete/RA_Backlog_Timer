"""API routes for preferences, annotation, planning, sessions and launching."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException

from .. import calibration as calib_module
from .. import launcher as launch_module
from .. import planning, preferences, rarity
from ..config import log
from ..efficiency import add_metrics, effective_hours
from ..launcher import LaunchError, LaunchRoots
from ..storage import annotations, repo

# Populated by app.configure(); a request never supplies a filesystem path.
_launch_roots = LaunchRoots()


def set_launch_roots(values) -> None:
    global _launch_roots
    _launch_roots = LaunchRoots.from_strings(values)
    log.debug("ROM roots: %s", [str(r) for r in _launch_roots.roots])


def get_launch_roots() -> LaunchRoots:
    return _launch_roots


def _enriched(conn: sqlite3.Connection) -> pd.DataFrame:
    """Games joined with everything the UI needs, under current preferences."""
    prefs = preferences.load(conn)
    df = repo.load_games(conn)
    if df.empty:
        return df

    df = add_metrics(df, prefs.time_columns, prefs.earned_column)

    ann = annotations.load_annotations(conn)
    if not ann.empty:
        df = df.merge(ann, on="ra_id", how="left")
    for col, default in (("pinned", 0), ("hidden", 0), ("priority", None),
                         ("note", None)):
        if col not in df.columns:
            df[col] = default
    df["pinned"] = df["pinned"].fillna(0).astype(int)
    df["hidden"] = df["hidden"].fillna(0).astype(int)

    tags = annotations.tags_for_games(conn)
    df["tags"] = df["ra_id"].map(lambda i: tags.get(int(i), []))

    logged = annotations.logged_hours_by_game(conn)
    df["logged_hours"] = df["ra_id"].map(lambda i: logged.get(int(i)))

    if "rarity_score" in df.columns:
        df["adjusted_points_per_hour"] = rarity.adjusted_points_per_hour(
            df["points_per_hour"], df["rarity_score"])
        df["realistic_hours"] = rarity.realistic_hours(
            df["effective_hours"], df["rarity_score"])

    if prefs.use_calibration:
        cal = _calibration(conn)
        if cal.confident:
            df["calibrated_hours"] = cal.apply(df["effective_hours"])
    return df


def _calibration(conn: sqlite3.Connection) -> calib_module.Calibration:
    """Compare logged time against estimates, for finished games only."""
    prefs = preferences.load(conn)
    df = repo.load_games(conn)
    if df.empty:
        return calib_module.Calibration()

    hours = effective_hours(df, prefs.time_columns)
    logged = annotations.logged_hours_by_game(conn)
    total = pd.to_numeric(df["achievements"], errors="coerce")
    earned = pd.to_numeric(df[prefs.earned_column], errors="coerce").fillna(0)

    observations = []
    for i, ra_id in enumerate(df["ra_id"]):
        played = logged.get(int(ra_id))
        if not played:
            continue
        # Only completed games: a half-played game logs fewer hours for
        # reasons unrelated to pace, and would bias the factor downwards.
        if not (total.iloc[i] and earned.iloc[i] >= total.iloc[i]):
            continue
        observations.append({"logged_hours": played,
                             "estimated_hours": hours.iloc[i]})
    return calib_module.compute(observations)


# --- preferences (#1, #2) ---------------------------------------------------

def _routes(get_conn) -> APIRouter:
    """Build a router bound to the app's connection dependency.

    A fresh APIRouter per call: a module-level one accumulates duplicate
    routes if this is ever invoked more than once (tests, reloads).
    """
    router = APIRouter()

    @router.get("/api/preferences")
    async def get_preferences(conn: sqlite3.Connection = Depends(get_conn)):
        return preferences.load(conn).to_dict()

    @router.post("/api/preferences")
    async def set_preferences(payload: dict,
                              conn: sqlite3.Connection = Depends(get_conn)):
        return preferences.update(conn, **(payload or {})).to_dict()

    # --- annotation (#4) ----------------------------------------------------

    @router.post("/api/games/{ra_id}/annotation")
    async def annotate(ra_id: int, payload: dict,
                       conn: sqlite3.Connection = Depends(get_conn)):
        annotations.set_annotation(conn, ra_id, **(payload or {}))
        return {"ok": True}

    @router.get("/api/tags")
    async def get_tags(conn: sqlite3.Connection = Depends(get_conn)):
        return {"tags": annotations.list_tags(conn)}

    @router.post("/api/games/{ra_id}/tags")
    async def add_tag(ra_id: int, payload: dict,
                      conn: sqlite3.Connection = Depends(get_conn)):
        name = (payload or {}).get("name", "").strip()
        if not name:
            raise HTTPException(400, "tag name is required")
        return {"tag_id": annotations.tag_game(conn, ra_id, name)}

    @router.delete("/api/games/{ra_id}/tags/{tag_id}")
    async def remove_tag(ra_id: int, tag_id: int,
                         conn: sqlite3.Connection = Depends(get_conn)):
        annotations.untag_game(conn, ra_id, tag_id)
        return {"ok": True}

    # --- saved views (#5) ---------------------------------------------------

    @router.get("/api/views")
    async def get_views(conn: sqlite3.Connection = Depends(get_conn)):
        return {"views": annotations.list_views(conn)}

    @router.post("/api/views")
    async def create_view(payload: dict,
                          conn: sqlite3.Connection = Depends(get_conn)):
        name = (payload or {}).get("name", "").strip()
        if not name:
            raise HTTPException(400, "view name is required")
        return {"view_id": annotations.save_view(conn, name,
                                                 payload.get("filters", {}))}

    @router.delete("/api/views/{view_id}")
    async def remove_view(view_id: int,
                          conn: sqlite3.Connection = Depends(get_conn)):
        annotations.delete_view(conn, view_id)
        return {"ok": True}

    # --- nearly there (#3) --------------------------------------------------

    @router.get("/api/nearly-there")
    async def nearly_there(limit: int = 25, max_remaining: int = 5,
                           conn: sqlite3.Connection = Depends(get_conn)):
        df = _enriched(conn)
        if df.empty or "remaining_achievements" not in df.columns:
            return {"games": []}
        near = df[(df["remaining_achievements"] > 0)
                  & (df["remaining_achievements"] <= max_remaining)]
        near = near.sort_values("remaining_achievements").head(limit)
        return {"games": _records(near)}

    # --- roulette (#6) ------------------------------------------------------

    @router.get("/api/roulette")
    async def spin(max_hours: float | None = None, min_hours: float | None = None,
                   max_rarity: float | None = None, system: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)):
        prefs = preferences.load(conn)
        df = _enriched(conn)
        if df.empty:
            return {"game": None, "candidates": 0}
        result = planning.roulette(
            df, max_hours=max_hours, min_hours=min_hours, max_rarity=max_rarity,
            systems=[system] if system else None, columns=prefs.time_columns)
        return {"game": result.game, "candidates": result.candidates}

    # --- points target (#8) -------------------------------------------------

    @router.get("/api/points-target")
    async def points_target(target: int, conn: sqlite3.Connection = Depends(get_conn)):
        if target <= 0:
            raise HTTPException(400, "target must be positive")
        prefs = preferences.load(conn)
        plan = planning.plan_for_points(_enriched(conn), target,
                                        columns=prefs.time_columns)
        return asdict(plan)

    # --- achievements (#9) --------------------------------------------------

    @router.get("/api/games/{ra_id}/achievements")
    async def game_achievements(ra_id: int,
                                conn: sqlite3.Connection = Depends(get_conn)):
        rows = annotations.achievements_for(conn, ra_id)
        for row in rows:
            row["earn_rate"] = rarity.earn_rate(row.get("num_awarded"),
                                                row.get("total_players"))
            row["band"] = rarity.band(row["earn_rate"])
            row["earned"] = bool(row.get("earned_at"))
        summary = rarity.analyse(rows) if rows else None
        return {
            "achievements": rows,
            "summary": asdict(summary) if summary else None,
        }

    # --- new sets (#10) -----------------------------------------------------

    @router.get("/api/new-sets")
    async def new_sets(conn: sqlite3.Connection = Depends(get_conn)):
        """Games on your list that have no achievement set yet, plus any that
        gained one since the last scan."""
        df = repo.load_games(conn)
        if df.empty:
            return {"awaiting": [], "newly_published": []}
        awaiting = df[df["achievements"].fillna(0) == 0]
        published = df[(df["set_published"] == 0) & (df["achievements"] > 0)]
        return {
            "awaiting": _records(awaiting[["ra_id", "title", "system"]]),
            "newly_published": _records(
                published[["ra_id", "title", "system", "achievements", "points"]]),
        }

    @router.post("/api/new-sets/acknowledge")
    async def acknowledge_sets(conn: sqlite3.Connection = Depends(get_conn)):
        from ..storage.db import transaction
        with transaction(conn):
            conn.execute("UPDATE games SET set_published=1 WHERE achievements>0")
            conn.execute("UPDATE games SET set_published=0 WHERE achievements=0")
        return {"ok": True}

    # --- events (#11) -------------------------------------------------------

    @router.get("/api/events")
    async def get_events(conn: sqlite3.Connection = Depends(get_conn)):
        events = annotations.active_events(conn)
        return {"events": events, "on_your_list": [e for e in events if e.get("title")]}

    @router.post("/api/events")
    async def set_events(payload: dict, conn: sqlite3.Connection = Depends(get_conn)):
        items = (payload or {}).get("events", [])
        if not isinstance(items, list):
            raise HTTPException(400, "events must be a list")
        return {"count": annotations.replace_events(conn, items)}

    # --- schedule (#12) -----------------------------------------------------

    @router.get("/api/schedule")
    async def get_schedule(weeks: int = 12, weekly_hours: float | None = None,
                           conn: sqlite3.Connection = Depends(get_conn)):
        prefs = preferences.load(conn)
        hours = weekly_hours or prefs.weekly_hours
        if hours <= 0:
            raise HTTPException(400, "weekly_hours must be positive")
        plan = planning.build_schedule(_enriched(conn), weekly_hours=hours,
                                       weeks=max(1, min(52, weeks)),
                                       columns=prefs.time_columns)
        return {
            "weeks": [asdict(w) for w in plan.weeks],
            "weekly_hours": plan.weekly_hours,
            "total_hours": plan.total_hours,
            "total_points": plan.total_points,
            "unscheduled": plan.unscheduled,
        }

    # --- sessions & calibration (#13) ---------------------------------------

    @router.get("/api/sessions")
    async def get_sessions(ra_id: int | None = None,
                           conn: sqlite3.Connection = Depends(get_conn)):
        sessions = annotations.sessions_for(conn, ra_id)
        cal = _calibration(conn)
        return {
            "sessions": sessions,
            "open": annotations.open_session(conn),
            "calibration": {**asdict(cal), "description": cal.description},
            "velocity": calib_module.velocity(sessions),
        }

    @router.post("/api/games/{ra_id}/sessions/start")
    async def session_start(ra_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        existing = annotations.open_session(conn)
        if existing:
            raise HTTPException(409, f"A session is already running for "
                                     f"{existing.get('title')}")
        return {"session_id": annotations.start_session(conn, ra_id)}

    @router.post("/api/sessions/{session_id}/finish")
    async def session_finish(session_id: int, payload: dict | None = None,
                             conn: sqlite3.Connection = Depends(get_conn)):
        payload = payload or {}
        annotations.finish_session(conn, session_id, payload.get("minutes"),
                                   payload.get("note"))
        return {"ok": True}

    @router.post("/api/games/{ra_id}/sessions")
    async def session_log(ra_id: int, payload: dict,
                          conn: sqlite3.Connection = Depends(get_conn)):
        minutes = (payload or {}).get("minutes")
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise HTTPException(400, "minutes must be a number")
        if minutes <= 0:
            raise HTTPException(400, "minutes must be positive")
        return {"session_id": annotations.log_session(
            conn, ra_id, minutes, (payload or {}).get("note"))}

    # --- launching (#15) ----------------------------------------------------

    @router.get("/api/launch/config")
    async def launch_config(conn: sqlite3.Connection = Depends(get_conn)):
        roots = get_launch_roots()
        return {
            "roots": [str(r) for r in roots.roots],
            "enabled": bool(roots.roots),
            "emulators": sorted(launch_module.KNOWN_EMULATORS),
            "targets": annotations.list_launch_targets(conn),
        }

    @router.post("/api/games/{ra_id}/launch-target")
    async def register_target(ra_id: int, payload: dict,
                              conn: sqlite3.Connection = Depends(get_conn)):
        path = (payload or {}).get("rom_path", "")
        try:
            rom = launch_module.validate_rom(path, get_launch_roots())
        except LaunchError as exc:
            raise HTTPException(400, str(exc))
        annotations.set_launch_target(
            conn, ra_id, str(rom), payload.get("emulator"),
            payload.get("core"), payload.get("extra_args"))
        return {"ok": True, "rom_path": str(rom)}

    @router.post("/api/games/{ra_id}/launch")
    async def launch_game(ra_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Start a *registered* ROM. The request carries an id, never a path."""
        target = annotations.get_launch_target(conn, ra_id)
        try:
            command = launch_module.launch(target, get_launch_roots())
        except LaunchError as exc:
            raise HTTPException(400, str(exc))
        session_id = annotations.start_session(conn, ra_id)
        return {"ok": True, "command": command, "session_id": session_id}

    @router.delete("/api/games/{ra_id}/launch-target")
    async def unregister_target(ra_id: int,
                                conn: sqlite3.Connection = Depends(get_conn)):
        annotations.delete_launch_target(conn, ra_id)
        return {"ok": True}

    return router


def _records(df: pd.DataFrame) -> list[dict]:
    from .app import _clean
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]
