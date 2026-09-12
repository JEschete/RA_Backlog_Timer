"""Command-line entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys

from ..config import (
    DB_FILE,
    resolve_db_path,
    DEFAULT_EXPORT,
    WEB_HOST,
    WEB_PORT,
    Colors,
    enable_windows_ansi,
    log,
    setup_logging,
)
from ..models import RAAuthError
from ..scanner import ScanOptions
from ..storage import export, migrate, repo
from ..storage.db import connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ra-backlog",
        description="Plan your RetroAchievements backlog with real completion times.",
    )
    parser.add_argument("--db", default=None,
                        help="database file (default: a shared per-user location; "
                             "see RA_BACKLOG_DB)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    parser.add_argument("--log-file", help="also write a debug log here")

    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="launch the browser dashboard (default)")
    web.add_argument("--host", default=WEB_HOST)
    web.add_argument("--port", type=int, default=WEB_PORT)
    web.add_argument("--no-browser", action="store_true", help="do not open a browser")
    web.add_argument("--rom-root", action="append", metavar="DIR",
                     help="folder containing your ROMs; repeatable. Launching is "
                          "disabled unless at least one is given")

    sub.add_parser("menu", help="interactive terminal menu")

    scan = sub.add_parser("scan", help="run a scan without any UI")
    scan.add_argument("--fresh", action="store_true", help="re-check every game")
    scan.add_argument("--systems", nargs="*", help="only these systems")
    scan.add_argument("--exclude", nargs="*", help="skip these systems")
    scan.add_argument("--no-user-progress", action="store_true",
                      help="skip fetching your earned achievements")
    scan.add_argument("--no-achievements", action="store_true",
                      help="skip per-achievement detail (rarity scores)")
    scan.add_argument("--achievement-limit", type=int, default=60,
                      help="how many games to fetch achievement detail for (default: 60)")

    exp = sub.add_parser("export", help="write an Excel or CSV file")
    exp.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    exp.add_argument("-o", "--output", default=DEFAULT_EXPORT)

    creds = sub.add_parser("credentials", help="manage stored credentials")
    creds.add_argument("--clear", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, quiet=args.quiet, logfile=args.log_file)
    enable_windows_ansi()

    db_path = resolve_db_path(args.db)
    log.debug("Using database %s", db_path)
    conn = connect(db_path)
    try:
        if migrate.needs_migration(conn):
            log.info("Importing your existing data...")
            stats = migrate.run(conn)
            log.info("  %s games, %s cached lookups", stats["games"], stats["lookups"])

        command = args.command or "web"

        if command == "web":
            from ..web.app import serve
            conn.close()          # the server manages its own connections
            serve(getattr(args, "host", WEB_HOST),
                  getattr(args, "port", WEB_PORT),
                  db_path=str(db_path),
                  open_browser=not getattr(args, "no_browser", False),
                  rom_roots=getattr(args, "rom_root", None) or [])
            return 0

        if command == "menu":
            from .menu import run
            asyncio.run(run(conn))
            return 0

        if command == "scan":
            return _run_scan(conn, args)

        if command == "export":
            return _run_export(conn, args)

        if command == "credentials":
            return _run_credentials(conn, args)

        parser.print_help()
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_scan(conn, args) -> int:
    from .menu import _progress_line, ensure_credentials
    from ..scanner import Scanner

    creds = ensure_credentials(conn)
    if creds is None:
        return 1

    options = ScanOptions(
        fresh=args.fresh,
        systems=args.systems or None,
        exclude_systems=args.exclude or None,
        fetch_user_progress=not args.no_user_progress,
        fetch_achievements=not args.no_achievements,
        achievement_limit=args.achievement_limit,
    )
    scanner = Scanner(conn, creds, options, progress_cb=_progress_line)
    try:
        result = asyncio.run(scanner.run())
    except RAAuthError as exc:
        print(f"{Colors.RED}{exc}{Colors.RESET}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Progress up to this point is saved.")
        return 130

    log.info("Done: %s fetched, %s cached, %s failed",
             result.fetched, result.from_cache, result.failed)
    return 0


def _run_export(conn, args) -> int:
    try:
        path = (export.to_excel(conn, args.output) if args.format == "xlsx"
                else export.to_csv(conn, args.output))
    except PermissionError:
        print(f"{Colors.RED}{args.output} is open in another program.{Colors.RESET}",
              file=sys.stderr)
        return 2
    log.info("Saved %s", path.resolve())
    return 0


def _run_credentials(conn, args) -> int:
    from .. import credentials as creds_module
    from .menu import ensure_credentials

    if args.clear:
        creds_module.clear()
        log.info("Credentials cleared.")
        return 0
    creds = ensure_credentials(conn, force=True)
    return 0 if creds else 1


if __name__ == "__main__":
    raise SystemExit(main())
