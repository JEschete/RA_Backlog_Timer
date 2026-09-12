"""User-authored data: tags, notes, pins, saved views, sessions, events, ROMs.

Kept apart from `repo` because the rule differs: nothing in here is ever
touched by a scan. Anything you typed survives every refresh.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

import pandas as pd

from .db import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- tags (#4) --------------------------------------------------------------

def create_tag(conn: sqlite3.Connection, name: str, colour: str | None = None) -> int:
    name = name.strip()
    if not name:
        raise ValueError("tag name cannot be empty")
    with transaction(conn):
        conn.execute(
            "INSERT INTO tags(name, colour) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET colour=COALESCE(excluded.colour, tags.colour)",
            (name, colour),
        )
    row = conn.execute("SELECT tag_id FROM tags WHERE name=?", (name,)).fetchone()
    return int(row["tag_id"])


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT t.tag_id, t.name, t.colour, COUNT(gt.ra_id) AS uses "
        "FROM tags t LEFT JOIN game_tags gt ON gt.tag_id = t.tag_id "
        "GROUP BY t.tag_id ORDER BY t.name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM tags WHERE tag_id=?", (tag_id,))


def tag_game(conn: sqlite3.Connection, ra_id: int, tag_name: str) -> int:
    tag_id = create_tag(conn, tag_name)
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO game_tags(ra_id, tag_id) VALUES(?, ?)",
            (ra_id, tag_id),
        )
    return tag_id


def untag_game(conn: sqlite3.Connection, ra_id: int, tag_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM game_tags WHERE ra_id=? AND tag_id=?", (ra_id, tag_id))


def tags_for_games(conn: sqlite3.Connection) -> dict[int, list[str]]:
    rows = conn.execute(
        "SELECT gt.ra_id, t.name FROM game_tags gt "
        "JOIN tags t ON t.tag_id = gt.tag_id ORDER BY t.name COLLATE NOCASE"
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["ra_id"]), []).append(r["name"])
    return out


# --- notes / pins / hide (#4) ----------------------------------------------

def set_annotation(conn: sqlite3.Connection, ra_id: int, **fields) -> None:
    allowed = {"note", "pinned", "hidden", "priority"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return

    columns = ", ".join(f"{k}=excluded.{k}" for k in updates)
    keys = ", ".join(["ra_id", *updates])
    placeholders = ", ".join("?" * (len(updates) + 1))
    values: list[Any] = [ra_id]
    for k in updates:
        v = updates[k]
        values.append(int(bool(v)) if k in ("pinned", "hidden") else v)

    with transaction(conn):
        conn.execute(
            f"INSERT INTO annotations({keys}) VALUES({placeholders}) "
            f"ON CONFLICT(ra_id) DO UPDATE SET {columns}, updated_at=datetime('now')",
            values,
        )


def load_annotations(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ra_id, note, pinned, hidden, priority FROM annotations", conn)


# --- saved views (#5) -------------------------------------------------------

def save_view(conn: sqlite3.Connection, name: str, payload: dict) -> int:
    name = name.strip()
    if not name:
        raise ValueError("view name cannot be empty")
    with transaction(conn):
        conn.execute(
            "INSERT INTO views(name, payload) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
            (name, json.dumps(payload)),
        )
    row = conn.execute("SELECT view_id FROM views WHERE name=?", (name,)).fetchone()
    return int(row["view_id"])


def list_views(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT view_id, name, payload, created_at FROM views ORDER BY name"
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            payload = {}
        out.append({"view_id": r["view_id"], "name": r["name"],
                    "payload": payload, "created_at": r["created_at"]})
    return out


def delete_view(conn: sqlite3.Connection, view_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM views WHERE view_id=?", (view_id,))


# --- achievements (#7, #9) --------------------------------------------------

def replace_achievements(conn: sqlite3.Connection, ra_id: int,
                         rows: Sequence[dict]) -> int:
    """Swap in a fresh achievement list for one game."""
    with transaction(conn):
        conn.execute("DELETE FROM achievements WHERE ra_id=?", (ra_id,))
        conn.executemany(
            "INSERT INTO achievements(achievement_id, ra_id, title, description, "
            "points, num_awarded, num_awarded_hc, total_players, earned_at, "
            "earned_hc_at, display_order, fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (r.get("achievement_id"), ra_id, r.get("title"), r.get("description"),
                 r.get("points", 0), r.get("num_awarded"), r.get("num_awarded_hc"),
                 r.get("total_players"), r.get("earned_at"), r.get("earned_hc_at"),
                 r.get("display_order"))
                for r in rows
            ],
        )
        conn.execute(
            "UPDATE games SET achievements_fetched_at=datetime('now') WHERE ra_id=?",
            (ra_id,))
    return len(rows)


def achievements_for(conn: sqlite3.Connection, ra_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT achievement_id, title, description, points, num_awarded, "
        "num_awarded_hc, total_players, earned_at, earned_hc_at "
        "FROM achievements WHERE ra_id=? ORDER BY display_order, achievement_id",
        (ra_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def store_rarity(conn: sqlite3.Connection, ra_id: int,
                 score: float | None, rarest_pct: float | None) -> None:
    with transaction(conn):
        conn.execute("UPDATE games SET rarity_score=?, rarest_pct=? WHERE ra_id=?",
                     (score, rarest_pct, ra_id))


def games_needing_achievements(conn: sqlite3.Connection, limit: int | None = None,
                               stale_days: int = 30) -> list[int]:
    sql = ("SELECT ra_id FROM games WHERE in_want_to_play=1 AND achievements>0 "
           "AND (achievements_fetched_at IS NULL "
           "     OR julianday('now') - julianday(achievements_fetched_at) > ?) "
           "ORDER BY points DESC")
    params: list[Any] = [stale_days]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [int(r["ra_id"]) for r in conn.execute(sql, params)]


# --- sessions (#13) ---------------------------------------------------------

def start_session(conn: sqlite3.Connection, ra_id: int) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions(ra_id, started_at) VALUES(?, ?)", (ra_id, _now()))
    return int(cur.lastrowid)


def finish_session(conn: sqlite3.Connection, session_id: int,
                   minutes: float | None = None, note: str | None = None) -> None:
    row = conn.execute(
        "SELECT started_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        return

    ended = _now()
    if minutes is None:
        try:
            started = datetime.fromisoformat(row["started_at"])
            minutes = round((datetime.fromisoformat(ended) - started).total_seconds() / 60, 1)
        except ValueError:
            minutes = 0.0

    with transaction(conn):
        conn.execute(
            "UPDATE sessions SET ended_at=?, minutes=?, note=COALESCE(?, note) "
            "WHERE session_id=?", (ended, minutes, note, session_id))


def log_session(conn: sqlite3.Connection, ra_id: int, minutes: float,
                note: str | None = None, started_at: str | None = None) -> int:
    """Record a session after the fact, for time you played before logging existed."""
    started = started_at or _now()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions(ra_id, started_at, ended_at, minutes, note) "
            "VALUES(?,?,?,?,?)", (ra_id, started, _now(), minutes, note))
    return int(cur.lastrowid)


def sessions_for(conn: sqlite3.Connection, ra_id: int | None = None) -> list[dict]:
    if ra_id is None:
        rows = conn.execute(
            "SELECT session_id, ra_id, started_at, ended_at, minutes, note "
            "FROM sessions ORDER BY started_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT session_id, ra_id, started_at, ended_at, minutes, note "
            "FROM sessions WHERE ra_id=? ORDER BY started_at DESC", (ra_id,)).fetchall()
    return [dict(r) for r in rows]


def logged_hours_by_game(conn: sqlite3.Connection) -> dict[int, float]:
    rows = conn.execute(
        "SELECT ra_id, SUM(minutes) AS total FROM sessions "
        "WHERE minutes IS NOT NULL GROUP BY ra_id").fetchall()
    return {int(r["ra_id"]): round((r["total"] or 0) / 60.0, 2) for r in rows}


def open_session(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT s.session_id, s.ra_id, s.started_at, g.title "
        "FROM sessions s JOIN games g ON g.ra_id = s.ra_id "
        "WHERE s.ended_at IS NULL ORDER BY s.started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --- events (#11) -----------------------------------------------------------

def replace_events(conn: sqlite3.Connection, events: Sequence[dict],
                   source: str = "manual") -> int:
    with transaction(conn):
        conn.execute("DELETE FROM events WHERE source=?", (source,))
        conn.executemany(
            "INSERT INTO events(name, ra_id, starts_at, ends_at, url, source) "
            "VALUES(?,?,?,?,?,?)",
            [(e.get("name"), e.get("ra_id"), e.get("starts_at"),
              e.get("ends_at"), e.get("url"), source) for e in events],
        )
    return len(events)


def active_events(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT e.event_id, e.name, e.ra_id, e.starts_at, e.ends_at, e.url, "
        "       e.source, g.title, g.system "
        "FROM events e LEFT JOIN games g ON g.ra_id = e.ra_id "
        "WHERE (e.ends_at IS NULL OR date(e.ends_at) >= date('now')) "
        "ORDER BY e.starts_at").fetchall()
    return [dict(r) for r in rows]


# --- launch targets (#15) ---------------------------------------------------

def set_launch_target(conn: sqlite3.Connection, ra_id: int, rom_path: str,
                      emulator: str | None = None, core: str | None = None,
                      extra_args: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO launch_targets(ra_id, rom_path, emulator, core, extra_args, "
            "verified_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(ra_id) DO UPDATE SET rom_path=excluded.rom_path, "
            "emulator=excluded.emulator, core=excluded.core, "
            "extra_args=excluded.extra_args, verified_at=datetime('now')",
            (ra_id, rom_path, emulator, core, extra_args),
        )


def get_launch_target(conn: sqlite3.Connection, ra_id: int) -> dict | None:
    row = conn.execute(
        "SELECT ra_id, rom_path, emulator, core, extra_args FROM launch_targets "
        "WHERE ra_id=?", (ra_id,)).fetchone()
    return dict(row) if row else None


def list_launch_targets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT ra_id, rom_path, emulator, core FROM launch_targets").fetchall()
    return [dict(r) for r in rows]


def delete_launch_target(conn: sqlite3.Connection, ra_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM launch_targets WHERE ra_id=?", (ra_id,))
