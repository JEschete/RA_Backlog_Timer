"""Constants, tunables and logging setup."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# --- Network tunables -------------------------------------------------------
DELAY_BETWEEN_REQUESTS = 0.3
MAX_CONCURRENT_REQUESTS = 5
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 0.8          # seconds; doubled each attempt, plus jitter
BACKOFF_CAP = 20.0

# --- Endpoints --------------------------------------------------------------
RA_API_BASE = "https://retroachievements.org/API"

# --- Storage ----------------------------------------------------------------

def default_data_dir() -> Path:
    """Per-user data directory, so every launch point shares one database.

    These used to be bare relative filenames, which meant the database lived
    wherever you happened to launch from: running from the project folder and
    running from your home directory silently produced two separate backlogs.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "RA_Backlog_Timer"


def resolve_db_path(explicit: str | os.PathLike | None = None) -> Path:
    """Where the database lives, in precedence order.

    1. an explicit --db argument
    2. the RA_BACKLOG_DB environment variable
    3. the per-user data directory

    A stray ./backlog.db from before this became a fixed location is *moved*
    into the data directory rather than used in place -- deferring to it would
    preserve exactly the cwd-dependence this function exists to remove.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("RA_BACKLOG_DB")
    if env:
        return Path(env).expanduser().resolve()

    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "backlog.db"

    legacy = Path.cwd() / "backlog.db"
    if legacy.exists() and not target.exists() and legacy.resolve() != target:
        _adopt_legacy_db(legacy, target)

    return target


def _adopt_legacy_db(legacy: Path, target: Path) -> None:
    """Move a pre-existing database (and its WAL sidecars) into the data dir."""
    import shutil
    try:
        shutil.move(str(legacy), str(target))
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(legacy) + suffix)
            if sidecar.exists():
                shutil.move(str(sidecar), str(target) + suffix)
        log.info("Moved your existing backlog.db to %s", target)
    except OSError as exc:
        # Locked by another instance, or a permissions problem. Leave it be and
        # carry on with a fresh database rather than failing to start.
        log.warning("Could not move %s to %s (%s); using %s", legacy, target, exc, target)


def resolve_creds_path() -> Path:
    """Fallback credential file, kept beside the database for the same reason."""
    legacy = Path.cwd() / ".ra_credentials.json"
    if legacy.exists():
        return legacy.resolve()
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / ".ra_credentials.json"


# Kept as the documented default; call resolve_db_path() to get the real one.
DB_FILE = "backlog.db"
DEFAULT_EXPORT = "HowLongToBeat.xlsx"   # exports stay relative to your cwd
CREDS_FILE = ".ra_credentials.json"
KEYRING_SERVICE = "RAHLTBScraper"

# Legacy files, read once by storage.migrate then left alone.
LEGACY_PROGRESS_FILE = "hltb_progress.json"
LEGACY_RA_CACHE_FILE = "ra_wanttoplay_cache.json"

# Failed lookups are retried once they age past this. Successes never expire.
ERROR_RETRY_TTL_HOURS = 24

# --- Web --------------------------------------------------------------------
WEB_HOST = "127.0.0.1"
WEB_PORT = 8000

log = logging.getLogger("ra_backlog")


class _ConsoleFormatter(logging.Formatter):
    """Plain messages at INFO, prefixed levels above it."""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno <= logging.INFO:
            return record.getMessage()
        return f"{record.levelname}: {record.getMessage()}"


def setup_logging(verbose: bool = False, quiet: bool = False,
                  logfile: str | os.PathLike | None = None) -> None:
    """Route UI output and diagnostics through logging (item #17)."""
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_ConsoleFormatter())
    log.addHandler(console)

    if logfile:
        fh = logging.FileHandler(Path(logfile), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s %(message)s"))
        log.addHandler(fh)

    log.propagate = False


def enable_windows_ansi() -> None:
    """Turn on VT processing so the ANSI colour codes render on Windows."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE, 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class Colors:
    RED = "\033[91m"
    ORANGE = "\033[93m"
    GREEN = "\033[92m"
    DIM = "\033[2m"
    RESET = "\033[0m"
