"""SQLite connection handling and schema migrations.

Replacing the xlsx-as-database arrangement is what makes items #3, #5 and #6
tractable: transactions are atomic by construction, the file is not held open
by Excel, and per-row cache status has somewhere to live.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import DB_FILE, log

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    ra_id               INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,
    system              TEXT DEFAULT '',
    achievements        INTEGER DEFAULT 0,
    points              INTEGER DEFAULT 0,

    hltb_beat           REAL,
    hltb_complete       REAL,
    hltb_name           TEXT,
    match_quality       TEXT,
    match_similarity    REAL,

    ra_beat             REAL,
    ra_master           REAL,
    ra_beat_hardcore    REAL,
    ra_master_hardcore  REAL,
    ra_players          INTEGER,

    earned              INTEGER,
    earned_hardcore     INTEGER,

    points_per_hour     REAL,
    in_want_to_play     INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_games_system  ON games(system);
CREATE INDEX IF NOT EXISTS idx_games_wtp     ON games(in_want_to_play);
CREATE INDEX IF NOT EXISTS idx_games_pph     ON games(points_per_hour);

-- Replaces hltb_progress.json. `status` is the whole point (item #3): the old
-- cache could not distinguish "HLTB has no such game" from "the network blipped",
-- so one transient failure poisoned a game permanently.
CREATE TABLE IF NOT EXISTS lookups (
    cache_key   TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- ok | no_match | error
    error       TEXT,
    payload     TEXT,                   -- JSON blob of the LookupResult
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lookups_status ON lookups(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# --- v2: per-game detail, user annotation, planning and play tracking -------
_SCHEMA_V2 = """
-- Per-achievement detail. Powers the drill-down view and the rarity score.
CREATE TABLE IF NOT EXISTS achievements (
    achievement_id  INTEGER PRIMARY KEY,
    ra_id           INTEGER NOT NULL,
    title           TEXT,
    description     TEXT,
    points          INTEGER DEFAULT 0,
    num_awarded     INTEGER,          -- softcore earners
    num_awarded_hc  INTEGER,          -- hardcore earners
    total_players   INTEGER,
    earned_at       TEXT,             -- when *you* earned it, NULL if not
    earned_hc_at    TEXT,
    display_order   INTEGER,
    fetched_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ra_id) REFERENCES games(ra_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ach_game ON achievements(ra_id);

-- User annotation. Deliberately separate from games so a rescan never
-- touches anything you typed.
CREATE TABLE IF NOT EXISTS tags (
    tag_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    colour  TEXT
);
CREATE TABLE IF NOT EXISTS game_tags (
    ra_id  INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (ra_id, tag_id),
    FOREIGN KEY (ra_id)  REFERENCES games(ra_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS annotations (
    ra_id      INTEGER PRIMARY KEY,
    note       TEXT,
    pinned     INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0,
    priority   INTEGER,               -- manual override, lower sorts first
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ra_id) REFERENCES games(ra_id) ON DELETE CASCADE
);

-- Named filter combinations.
CREATE TABLE IF NOT EXISTS views (
    view_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,         -- JSON filter state
    created_at TEXT DEFAULT (datetime('now'))
);

-- Logged play sessions, used to calibrate estimates against reality.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ra_id       INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    minutes     REAL,
    note        TEXT,
    FOREIGN KEY (ra_id) REFERENCES games(ra_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_game ON sessions(ra_id);

-- Generated multi-week schedules.
CREATE TABLE IF NOT EXISTS schedule_slots (
    slot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ra_id       INTEGER NOT NULL,
    week_start  TEXT NOT NULL,
    hours       REAL NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (ra_id) REFERENCES games(ra_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_slots_week ON schedule_slots(week_start);

-- Community events (Achievement of the Week and friends). Populated from a
-- config file or by hand -- RA has no documented public endpoint for these.
CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    ra_id      INTEGER,
    starts_at  TEXT,
    ends_at    TEXT,
    url        TEXT,
    source     TEXT DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_events_game ON events(ra_id);

-- Registered ROMs. The launcher only ever runs an entry from this table --
-- never a path supplied in a request.
CREATE TABLE IF NOT EXISTS launch_targets (
    ra_id       INTEGER PRIMARY KEY,
    rom_path    TEXT NOT NULL,
    emulator    TEXT,
    core        TEXT,
    extra_args  TEXT,
    verified_at TEXT,
    FOREIGN KEY (ra_id) REFERENCES games(ra_id) ON DELETE CASCADE
);
"""

# Columns added to `games` in v2, applied one at a time so a partially
# migrated database can be brought forward without error.
_V2_GAME_COLUMNS = (
    ("rarity_score", "REAL"),        # weighted difficulty from achievement rarity
    ("rarest_pct", "REAL"),          # earn rate of the hardest achievement
    ("achievements_fetched_at", "TEXT"),
    ("set_published", "INTEGER"),    # 0 = on your list but no achievement set yet
    ("first_seen_at", "TEXT"),
)


def connect(path: str | Path = DB_FILE) -> sqlite3.Connection:
    """Open the database, applying schema migrations as needed."""
    path = Path(path)
    # check_same_thread=False: FastAPI resolves sync dependencies on a worker
    # thread while async routes run on the event loop, so a connection legally
    # crosses threads. Access stays serialized (one connection per request, and
    # the scanner owns its own), and WAL handles cross-connection locking.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the web UI read while a scan writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return

    if current == 0:
        conn.executescript(_SCHEMA)
        log.debug("Initialised schema")

    if current < 2:
        conn.executescript(_SCHEMA_V2)
        _add_missing_columns(conn, "games", _V2_GAME_COLUMNS)
        log.debug("Applied schema v2")

    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    if current:
        log.debug("Migrated schema %s -> %s", current, SCHEMA_VERSION)


def _add_missing_columns(conn: sqlite3.Connection, table: str,
                         columns: tuple[tuple[str, str], ...]) -> None:
    """ALTER TABLE ADD COLUMN for anything not already present.

    SQLite has no ADD COLUMN IF NOT EXISTS, and re-running a migration over a
    partially-upgraded database is a normal thing to happen.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


class transaction:
    """Context manager giving an all-or-nothing write (item #5).

    A Ctrl+C midway leaves the database exactly as it was, rather than the
    truncated JSON the old `json.dump` straight-to-file could produce.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
