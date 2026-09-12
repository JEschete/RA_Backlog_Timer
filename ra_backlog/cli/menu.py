"""Interactive terminal menu."""
from __future__ import annotations

import asyncio
import sqlite3

from ..config import Colors, DEFAULT_EXPORT, log
from ..credentials import Credentials
from .. import credentials as creds_module
from ..efficiency import add_metrics, plan_session, summarize
from ..models import RAAuthError
from ..scanner import ScanOptions, Scanner
from ..storage import export, repo
from .dialog import prompt_for_credentials

MENU = """
======================================================================
  RA Backlog Timer
======================================================================
  1. Update scan (new games only)
  2. Fresh scan (re-check everything)
  3. Scan specific systems
  4. Backlog summary
  5. Session planner
  6. Export to Excel
  7. Export to CSV
  8. Retry failed lookups
  9. Credentials
  0. Quit
"""


def _progress_line(progress) -> None:
    if progress.state != "running":
        return
    if progress.total:
        prefix = f"[{progress.done}/{progress.total}]"
    else:
        prefix = "  "
    title = f" {progress.title}" if progress.title else ""
    print(f"{prefix}{title} {Colors.DIM}{progress.detail}{Colors.RESET}")


def ensure_credentials(conn: sqlite3.Connection, force: bool = False) -> Credentials | None:
    creds = None if force else creds_module.load()
    if creds and creds.is_complete():
        return creds

    existing = repo.get_meta(conn, "username") or ""
    creds = prompt_for_credentials(existing)
    if creds is None:
        print(f"{Colors.ORANGE}Cancelled.{Colors.RESET}")
        return None
    creds_module.save(creds)
    print(f"{Colors.GREEN}Credentials saved to the {creds_module.storage_description()}.{Colors.RESET}")
    return creds


async def _scan(conn: sqlite3.Connection, creds: Credentials, options: ScanOptions) -> None:
    scanner = Scanner(conn, creds, options, progress_cb=_progress_line)
    try:
        result = await scanner.run()
    except RAAuthError as exc:
        # Item #8: an auth failure returns to the menu instead of killing the process.
        print(f"\n{Colors.RED}{exc}{Colors.RESET}")
        print("Pick option 9 to re-enter your credentials.")
        return
    except Exception as exc:
        print(f"\n{Colors.RED}Scan failed: {exc}{Colors.RESET}")
        return

    print("-" * 70)
    print(f"  Total games:   {result.total}")
    print(f"  Fetched:       {result.fetched}")
    print(f"  From cache:    {result.from_cache}")
    print(f"  No HLTB match: {result.no_match}")
    if result.failed:
        print(f"  {Colors.ORANGE}Failed:        {result.failed} "
              f"(retried automatically on the next run){Colors.RESET}")


def _show_summary(conn: sqlite3.Connection) -> None:
    df = add_metrics(repo.load_games(conn))
    if df.empty:
        print(f"\n{Colors.ORANGE}No data yet. Run a scan first.{Colors.RESET}")
        return

    s = summarize(df)
    print(f"\n  Games:                {s['total_games']}")
    print(f"  With HLTB data:       {s['with_hltb']}")
    print(f"  With RA mastery data: {s['with_ra_mastery']}")
    print(f"  Without time data:    {s['no_time_data']}")
    print(f"  Total mastery time:   {s['total_hours']:.1f} h "
          f"({s['total_hours'] / 24:.1f} days)")
    print(f"  Average per game:     {s['avg_hours']:.1f} h")

    print("\n  Most efficient games:")
    top = df.dropna(subset=["points_per_hour"]).nlargest(10, "points_per_hour")
    for row in top.itertuples():
        print(f"    {row.points_per_hour:7.1f} pts/hr  {row.title} "
              f"({row.points} pts, {row.effective_hours:.1f}h)")

    print("\n  Games by system:")
    for name, count in repo.list_systems(conn)[:10]:
        print(f"    {count:4d}  {name}")


def _run_planner(conn: sqlite3.Connection) -> None:
    raw = input("\n  Hours available: ").strip()
    try:
        budget = float(raw)
    except ValueError:
        print(f"{Colors.RED}Not a number.{Colors.RESET}")
        return

    plan = plan_session(repo.load_games(conn), budget_hours=budget)
    if not plan.games:
        print(f"{Colors.ORANGE}Nothing fits in that budget.{Colors.RESET}")
        return

    print(f"\n  {Colors.GREEN}{plan.total_points} points in {plan.total_hours:.1f} h"
          f"  ({plan.efficiency} pts/hr){Colors.RESET}\n")
    for g in plan.games:
        print(f"    {g.hours:6.1f}h  {g.points:5d}p  {g.title}")


def _choose_systems(conn: sqlite3.Connection) -> list[str] | None:
    systems = repo.list_systems(conn)
    if not systems:
        print(f"{Colors.ORANGE}No games yet -- run a scan first.{Colors.RESET}")
        return None

    print("\n  Available systems:")
    for i, (name, count) in enumerate(systems, 1):
        print(f"    {i:2d}. {name} ({count})")
    raw = input("\n  Numbers to include (comma-separated), or blank to cancel: ").strip()
    if not raw:
        return None
    try:
        picks = [systems[int(x.strip()) - 1][0] for x in raw.split(",")]
    except (ValueError, IndexError):
        print(f"{Colors.RED}Invalid selection.{Colors.RESET}")
        return None
    return picks


def _export(conn: sqlite3.Connection, fmt: str) -> None:
    try:
        path = (export.to_excel(conn, DEFAULT_EXPORT) if fmt == "xlsx"
                else export.to_csv(conn, DEFAULT_EXPORT))
        print(f"{Colors.GREEN}Saved to {path.resolve()}{Colors.RESET}")
    except PermissionError:
        # Item #6: a workbook open in Excel is a message, not a crash.
        print(f"{Colors.RED}{DEFAULT_EXPORT} is open in another program. "
              f"Close it and try again.{Colors.RESET}")
    except Exception as exc:
        print(f"{Colors.RED}Export failed: {exc}{Colors.RESET}")


async def run(conn: sqlite3.Connection) -> None:
    creds = creds_module.load()

    while True:
        print(MENU)
        if creds:
            print(f"  Signed in as {Colors.GREEN}{creds.username}{Colors.RESET}")
        choice = input("\n  Choice: ").strip()

        if choice == "0":
            print("\n  Bye.")
            return

        if choice in {"1", "2", "3"}:
            creds = creds or ensure_credentials(conn)
            if not creds:
                continue
            options = ScanOptions(fresh=(choice == "2"))
            if choice == "3":
                systems = _choose_systems(conn)
                if not systems:
                    continue
                options.systems = systems
            await _scan(conn, creds, options)

        elif choice == "4":
            _show_summary(conn)
        elif choice == "5":
            _run_planner(conn)
        elif choice == "6":
            _export(conn, "xlsx")
        elif choice == "7":
            _export(conn, "csv")
        elif choice == "8":
            cleared = repo.clear_failed_lookups(conn)
            print(f"  Cleared {cleared} failed lookup(s); "
                  f"they will be retried on the next scan.")
        elif choice == "9":
            creds = ensure_credentials(conn, force=True)
        else:
            print(f"{Colors.RED}  Not an option.{Colors.RESET}")

        input("\n  Press Enter to continue...")
