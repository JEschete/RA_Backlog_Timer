"""One-shot import of the pre-SQLite data files.

Reads whatever exists of `HowLongToBeat.xlsx`, `hltb_progress.json` and
`ra_wanttoplay_cache.json` into the database, then records that it ran so it
never touches them again. The legacy files are left on disk untouched.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from ..config import (
    DEFAULT_EXPORT,
    LEGACY_PROGRESS_FILE,
    LEGACY_RA_CACHE_FILE,
    log,
)
from ..models import Game, LookupResult
from . import repo
from .db import transaction

MIGRATION_KEY = "legacy_import_done"

# Old Excel header -> new column name.
_LEGACY_COLUMNS = {
    "Title": "title",
    "System": "system",
    "Achievements": "achievements",
    "Points": "points",
    "RA_ID": "ra_id",
    "HLTB_Beat": "hltb_beat",
    "HLTB_Complete": "hltb_complete",
    "RA_Beat": "ra_beat",
    "RA_Master": "ra_master",
    "RA_Players": "ra_players",
    "Points_Per_Hour": "points_per_hour",
}


def needs_migration(conn: sqlite3.Connection) -> bool:
    if repo.get_meta(conn, MIGRATION_KEY) == "1":
        return False
    return any(Path(p).exists() for p in
               (DEFAULT_EXPORT, LEGACY_PROGRESS_FILE, LEGACY_RA_CACHE_FILE))


def run(conn: sqlite3.Connection, excel_path: str | Path = DEFAULT_EXPORT) -> dict:
    """Import legacy data. Safe to call repeatedly; a no-op once done."""
    stats = {"games": 0, "lookups": 0, "source": None}

    excel_path = Path(excel_path)
    if excel_path.exists():
        stats["games"] = _import_excel(conn, excel_path)
        stats["source"] = str(excel_path)
    elif Path(LEGACY_RA_CACHE_FILE).exists():
        stats["games"] = _import_ra_cache(conn, Path(LEGACY_RA_CACHE_FILE))
        stats["source"] = LEGACY_RA_CACHE_FILE

    progress_path = Path(LEGACY_PROGRESS_FILE)
    if progress_path.exists():
        stats["lookups"] = _import_progress(conn, progress_path)

    repo.set_meta(conn, MIGRATION_KEY, "1")
    log.info("Imported %s games and %s cached lookups from legacy files",
             stats["games"], stats["lookups"])
    return stats


def _import_excel(conn: sqlite3.Connection, path: Path) -> int:
    try:
        df = pd.read_excel(path)
    except Exception as exc:
        log.warning("Could not read %s: %s", path, exc)
        return 0

    df = df.rename(columns=_LEGACY_COLUMNS)
    if "ra_id" not in df.columns or "title" not in df.columns:
        log.warning("%s does not look like a backlog export; skipping", path)
        return 0

    df = df[df["ra_id"].notna()]
    rows = []
    for r in df.itertuples():
        rows.append((
            int(r.ra_id),
            str(getattr(r, "title", "") or ""),
            str(getattr(r, "system", "") or ""),
            _int(getattr(r, "achievements", 0)),
            _int(getattr(r, "points", 0)),
            _float(getattr(r, "hltb_beat", None)),
            _float(getattr(r, "hltb_complete", None)),
            _float(getattr(r, "ra_beat", None)),
            _float(getattr(r, "ra_master", None)),
            _int_or_none(getattr(r, "ra_players", None)),
        ))

    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO games (ra_id, title, system, achievements, points,
                               hltb_beat, hltb_complete, ra_beat, ra_master,
                               ra_players, in_want_to_play)
            VALUES (?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(ra_id) DO UPDATE SET
                title=excluded.title, system=excluded.system,
                achievements=excluded.achievements, points=excluded.points,
                hltb_beat=COALESCE(excluded.hltb_beat, games.hltb_beat),
                hltb_complete=COALESCE(excluded.hltb_complete, games.hltb_complete),
                ra_beat=COALESCE(excluded.ra_beat, games.ra_beat),
                ra_master=COALESCE(excluded.ra_master, games.ra_master),
                ra_players=COALESCE(excluded.ra_players, games.ra_players)
            """,
            rows,
        )
    return len(rows)


def _import_ra_cache(conn: sqlite3.Connection, path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read %s: %s", path, exc)
        return 0

    games = [
        Game(
            ra_id=int(g["ID"]),
            title=g.get("Title", ""),
            system=g.get("ConsoleName", ""),
            achievements=_int(g.get("AchievementsPublished", 0)),
            points=_int(g.get("PointsTotal", 0)),
        )
        for g in data.get("games", []) if g.get("ID")
    ]
    if username := data.get("username"):
        repo.set_meta(conn, "username", username)
    return repo.upsert_games(conn, games)


def _import_progress(conn: sqlite3.Connection, path: Path) -> int:
    """Import hltb_progress.json.

    Every legacy entry becomes `ok` or `no_match`; the old format had no way to
    record that an entry was a transient failure, which is exactly the defect
    item #3 addresses. Anything that looks like an error is dropped so it gets
    retried rather than imported as a permanent negative.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read %s (%s); starting with an empty cache", path, exc)
        return 0

    imported = 0
    for cache_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("error"):
            continue                      # retry rather than inherit the failure
        has_match = bool(entry.get("hltb_name"))
        result = LookupResult(
            status="ok" if has_match else "no_match",
            beat=_float(entry.get("beat")),
            complete=_float(entry.get("complete")),
            hltb_name=entry.get("hltb_name"),
            similarity=float(entry.get("similarity") or 0.0),
            quality=_quality_from_comment(entry.get("comment"), has_match),
            comment=entry.get("comment"),
        )
        repo.put_lookup(conn, cache_key, result)
        imported += 1
    return imported


def _quality_from_comment(comment: str | None, has_match: bool) -> str:
    if not has_match:
        return "none"
    if not comment:
        return "exact"
    low = comment.lower()
    if low.startswith("fuzzy"):
        return "fuzzy"
    if low.startswith("loose"):
        return "loose"
    if low.startswith("poor"):
        return "poor"
    return "exact"


def _float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _int(value) -> int:
    f = _float(value)
    return int(f) if f is not None else 0


def _int_or_none(value) -> int | None:
    f = _float(value)
    return int(f) if f is not None else None
