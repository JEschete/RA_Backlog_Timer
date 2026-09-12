"""Queries over the backlog database."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import pandas as pd

from ..config import ERROR_RETRY_TTL_HOURS
from ..models import Game, LookupResult, ProgressionResult
from .db import transaction

_GAME_COLUMNS = (
    "ra_id", "title", "system", "achievements", "points",
    "hltb_beat", "hltb_complete", "hltb_name", "match_quality", "match_similarity",
    "ra_beat", "ra_master", "ra_beat_hardcore", "ra_master_hardcore", "ra_players",
    "earned", "earned_hardcore", "points_per_hour", "in_want_to_play",
    # v2
    "rarity_score", "rarest_pct", "achievements_fetched_at",
    "set_published", "first_seen_at",
)


# --- meta -------------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# --- games ------------------------------------------------------------------

def upsert_games(conn: sqlite3.Connection, games: Iterable[Game]) -> int:
    """Insert or update games, preserving any timing data already fetched."""
    rows = [(g.ra_id, g.title, g.system, g.achievements, g.points) for g in games]
    if not rows:
        return 0
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO games (ra_id, title, system, achievements, points, in_want_to_play)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(ra_id) DO UPDATE SET
                title           = excluded.title,
                system          = excluded.system,
                achievements    = excluded.achievements,
                points          = excluded.points,
                in_want_to_play = 1,
                updated_at      = datetime('now')
            """,
            rows,
        )
    return len(rows)


def mark_want_to_play(conn: sqlite3.Connection, ra_ids: Sequence[int]) -> int:
    """Flag games no longer on the list instead of deleting them.

    Dropping a game from the RA list used to lose every time value ever
    fetched for it; flagging means it comes back intact if re-added.
    """
    with transaction(conn):
        if ra_ids:
            placeholders = ",".join("?" * len(ra_ids))
            cur = conn.execute(
                "UPDATE games SET in_want_to_play=0 "
                "WHERE in_want_to_play=1 AND ra_id NOT IN (" + placeholders + ")",
                list(ra_ids),
            )
        else:
            cur = conn.execute("UPDATE games SET in_want_to_play=0")
    return cur.rowcount


def update_game_times(conn: sqlite3.Connection, ra_id: int,
                      lookup: LookupResult | None = None,
                      prog: ProgressionResult | None = None,
                      earned: int | None = None,
                      earned_hardcore: int | None = None) -> None:
    """Write fetched timings for one game. Only non-None fields are touched."""
    sets: list[str] = []
    params: list[Any] = []

    if lookup is not None and lookup.status != "error":
        sets += ["hltb_beat=?", "hltb_complete=?", "hltb_name=?",
                 "match_quality=?", "match_similarity=?"]
        params += [lookup.beat, lookup.complete, lookup.hltb_name,
                   lookup.quality, lookup.similarity]

    if prog is not None and prog.status == "ok":
        # Item #11: the hardcore columns were fetched and cached by the old
        # code but never written anywhere the user could see them.
        sets += ["ra_beat=?", "ra_master=?", "ra_beat_hardcore=?",
                 "ra_master_hardcore=?", "ra_players=?"]
        params += [prog.ra_beat, prog.ra_master, prog.ra_beat_hardcore,
                   prog.ra_master_hardcore, prog.ra_players]

    if earned is not None:
        sets += ["earned=?"]
        params += [earned]
    if earned_hardcore is not None:
        sets += ["earned_hardcore=?"]
        params += [earned_hardcore]

    if not sets:
        return

    sets.append("updated_at=datetime('now')")
    params.append(ra_id)
    with transaction(conn):
        conn.execute("UPDATE games SET " + ", ".join(sets) + " WHERE ra_id=?", params)


def store_points_per_hour(conn: sqlite3.Connection, values: dict) -> None:
    if not values:
        return
    with transaction(conn):
        conn.executemany(
            "UPDATE games SET points_per_hour=? WHERE ra_id=?",
            [(v, k) for k, v in values.items()],
        )


def load_games(conn: sqlite3.Connection, *,
               only_want_to_play: bool = True,
               systems: Sequence[str] | None = None) -> pd.DataFrame:
    sql = "SELECT " + ", ".join(_GAME_COLUMNS) + " FROM games"
    where: list[str] = []
    params: list[Any] = []
    if only_want_to_play:
        where.append("in_want_to_play=1")
    if systems:
        where.append("system IN (" + ",".join("?" * len(systems)) + ")")
        params += list(systems)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY title"

    df = pd.read_sql_query(sql, conn, params=params)
    for col in ("hltb_beat", "hltb_complete", "ra_beat", "ra_master",
                "ra_beat_hardcore", "ra_master_hardcore", "points_per_hour",
                "match_similarity", "rarity_score", "rarest_pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def list_systems(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT system, COUNT(*) AS n FROM games WHERE in_want_to_play=1 "
        "GROUP BY system ORDER BY n DESC"
    ).fetchall()
    return [(r["system"], r["n"]) for r in rows]


# --- lookup cache (item #3) -------------------------------------------------

def get_lookup(conn: sqlite3.Connection, cache_key: str) -> LookupResult | None:
    """Return a cached lookup, or None if absent or a stale failure.

    Successes and confirmed no-matches are kept indefinitely. Errors expire so
    a transient network failure stops being permanent.
    """
    row = conn.execute(
        "SELECT status, error, payload, fetched_at FROM lookups WHERE cache_key=?",
        (cache_key,),
    ).fetchone()
    if row is None:
        return None

    if row["status"] == "error":
        try:
            fetched = datetime.fromisoformat(row["fetched_at"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        if datetime.now(timezone.utc) - fetched > timedelta(hours=ERROR_RETRY_TTL_HOURS):
            return None          # expired -> caller retries
        return LookupResult(status="error", error=row["error"])

    payload = json.loads(row["payload"]) if row["payload"] else {}
    return LookupResult(**payload)


def put_lookup(conn: sqlite3.Connection, cache_key: str, result: LookupResult) -> None:
    payload = json.dumps({
        "status": result.status, "beat": result.beat, "complete": result.complete,
        "hltb_name": result.hltb_name, "similarity": result.similarity,
        "quality": result.quality, "comment": result.comment, "error": result.error,
    })
    with transaction(conn):
        conn.execute(
            "INSERT INTO lookups(cache_key, status, error, payload, fetched_at) "
            "VALUES(?,?,?,?,datetime('now')) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "status=excluded.status, error=excluded.error, "
            "payload=excluded.payload, fetched_at=excluded.fetched_at",
            (cache_key, result.status, result.error, payload),
        )


def clear_failed_lookups(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        cur = conn.execute("DELETE FROM lookups WHERE status='error'")
    return cur.rowcount


def cache_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM lookups GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
