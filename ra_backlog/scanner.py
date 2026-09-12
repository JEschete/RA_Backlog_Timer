"""Scan orchestration: fetch the list, look up times, persist as we go."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .clients import hltb
from .clients.ra import RAClient
from .config import DELAY_BETWEEN_REQUESTS, MAX_CONCURRENT_REQUESTS, log
from .credentials import Credentials
from .efficiency import compute_points_per_hour
from .models import LookupResult, ProgressionResult, RAAuthError, ScanProgress
from . import rarity
from .storage import annotations, repo
from .storage.db import transaction

ProgressCallback = Callable[[ScanProgress], None]


@dataclass
class ScanOptions:
    fresh: bool = False
    systems: Sequence[str] | None = None
    exclude_systems: Sequence[str] | None = None
    refresh_ra_times: bool = False      # re-fetch RA medians even if cached
    fetch_user_progress: bool = True    # item #19
    fetch_achievements: bool = True     # rarity + drill-down
    achievement_limit: int = 60         # per scan, to stay polite
    concurrency: int = MAX_CONCURRENT_REQUESTS


@dataclass
class ScanResult:
    total: int = 0
    fetched: int = 0
    from_cache: int = 0
    failed: int = 0
    no_match: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


class Scanner:
    """Runs one scan. Create a new instance per scan."""

    def __init__(self, conn: sqlite3.Connection, creds: Credentials,
                 options: ScanOptions | None = None,
                 progress_cb: ProgressCallback | None = None):
        self.conn = conn
        self.creds = creds
        self.options = options or ScanOptions()
        self.progress_cb = progress_cb
        self._cancelled = asyncio.Event()
        self.result = ScanResult()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _emit(self, done: int, total: int, title: str = "", detail: str = "",
              state: str = "running") -> None:
        if self.progress_cb:
            self.progress_cb(ScanProgress(
                done=done, total=total, title=title,
                detail=detail, state=state,
                errors=self.result.errors[-5:],
            ))

    async def run(self) -> ScanResult:
        try:
            return await self._run()
        except RAAuthError as exc:
            self.result.errors.append(str(exc))
            self._emit(0, 0, detail=str(exc), state="error")
            raise
        except Exception as exc:
            log.exception("Scan failed")
            self.result.errors.append(f"{type(exc).__name__}: {exc}")
            self._emit(0, 0, detail=str(exc), state="error")
            raise

    async def _run(self) -> ScanResult:
        async with RAClient(self.creds.api_key) as ra:
            # 1. Refresh the Want to Play list.
            self._emit(0, 0, detail="Fetching Want to Play list...")
            games = await ra.fetch_want_to_play(
                self.creds.username,
                on_page=lambda n, total: self._emit(
                    0, total, detail=f"Fetched {n}/{total} games..."),
            )
            if not games:
                self._emit(0, 0, detail="Want to Play list is empty", state="done")
                return self.result

            repo.upsert_games(self.conn, games)
            repo.mark_want_to_play(self.conn, [g.ra_id for g in games])
            repo.set_meta(self.conn, "username", self.creds.username)

            # 2. Earned achievements, so remaining time is meaningful (item #19).
            if self.options.fetch_user_progress:
                self._emit(0, len(games), detail="Fetching your achievement progress...")
                try:
                    progress = await ra.fetch_user_progress(
                        self.creds.username, [g.ra_id for g in games])
                    for ra_id, (earned, earned_hc) in progress.items():
                        repo.update_game_times(self.conn, ra_id,
                                               earned=earned, earned_hardcore=earned_hc)
                except RAAuthError:
                    raise
                except Exception as exc:
                    log.warning("Could not fetch user progress: %s", exc)

            # 2b. Achievement detail for the biggest sets: feeds the rarity
            # score and the per-achievement drill-down.
            if self.options.fetch_achievements:
                await self._fetch_achievements(ra)

            # 2c. Note which games gained an achievement set since last time.
            self._detect_new_sets()

            # 3. Work out what still needs looking up.
            pending = self._select_pending(games)
            self.result.total = len(games)
            self.result.from_cache = len(games) - len(pending)

            if not pending:
                self._finalize()
                self._emit(len(games), len(games),
                           detail="Everything already cached", state="done")
                return self.result

            self._emit(0, len(pending), detail=f"Looking up {len(pending)} games...")

            # 4. Fetch concurrently, persisting each result as it lands.
            semaphore = asyncio.Semaphore(self.options.concurrency)
            done = 0
            tasks = [self._process(g, ra, semaphore) for g in pending]

            for coro in asyncio.as_completed(tasks):
                game, lookup, prog = await coro
                done += 1

                if self.cancelled:
                    self.result.cancelled = True
                    break

                self._persist(game, lookup, prog)
                self._emit(done, len(pending), title=game.title,
                           detail=self._describe(lookup, prog))

        self._finalize()
        state = "cancelled" if self.result.cancelled else "done"
        self._emit(self.result.fetched + self.result.from_cache,
                   self.result.total, state=state)
        return self.result

    async def _fetch_achievements(self, ra: RAClient) -> None:
        pending = annotations.games_needing_achievements(
            self.conn, limit=self.options.achievement_limit)
        if not pending:
            return

        self._emit(0, len(pending), detail=f"Fetching achievement detail for "
                                           f"{len(pending)} games...")
        for i, ra_id in enumerate(pending, 1):
            if self.cancelled:
                return
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
            try:
                result = await ra.fetch_achievements(self.creds.username, ra_id)
            except RAAuthError:
                raise
            except Exception as exc:
                log.debug("Achievement fetch failed for %s: %s", ra_id, exc)
                continue

            rows = result.get("achievements") or []
            if not rows:
                continue
            annotations.replace_achievements(self.conn, ra_id, rows)
            difficulty = rarity.analyse(rows)
            annotations.store_rarity(self.conn, ra_id,
                                     difficulty.score, difficulty.rarest_pct)
            self._emit(i, len(pending), detail="achievement detail")

    def _detect_new_sets(self) -> None:
        """Flag games whose achievement set has just been published (item #10).

        A Want to Play entry with zero achievements is a set that does not
        exist yet. Recording that state means the next scan can tell you when
        one appears, which is a genuine reason to come back.
        """
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE games SET first_seen_at = COALESCE(first_seen_at, datetime('now')) "
                "WHERE in_want_to_play = 1")
            # NULL means "never assessed"; leave those alone so the first scan
            # after upgrading does not report the whole library as new.
            self.conn.execute(
                "UPDATE games SET set_published = 0 "
                "WHERE set_published IS NULL AND achievements = 0")
            self.conn.execute(
                "UPDATE games SET set_published = 1 "
                "WHERE set_published IS NULL AND achievements > 0")

    def _select_pending(self, games) -> list:
        """Games still needing a lookup.

        Item #3: a cached *error* whose TTL has expired comes back as a miss,
        so a transient network failure no longer sidelines a game permanently.
        """
        if self.options.fresh:
            return list(games)

        pending = []
        for game in games:
            if self.options.systems and game.system not in self.options.systems:
                continue
            if self.options.exclude_systems and game.system in self.options.exclude_systems:
                continue
            cached = repo.get_lookup(self.conn, hltb.cache_key(game.title, game.system))
            if cached is None or cached.status == "error":
                pending.append(game)
        return pending

    async def _process(self, game, ra: RAClient, semaphore: asyncio.Semaphore):
        async with semaphore:
            if self.cancelled:
                return game, LookupResult(status="error", error="cancelled"), ProgressionResult(status="error")

            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

            lookup = await hltb.search(game.title, game.system)
            prog = await ra.fetch_progression(game.ra_id)
            return game, lookup, prog

    def _persist(self, game, lookup: LookupResult, prog: ProgressionResult) -> None:
        key = hltb.cache_key(game.title, game.system)

        # Only durable outcomes are cached. Errors are written too, but with
        # status="error" so the TTL can expire them.
        repo.put_lookup(self.conn, key, lookup)
        repo.update_game_times(self.conn, game.ra_id, lookup=lookup, prog=prog)

        if lookup.status == "error":
            self.result.failed += 1
            self.result.errors.append(f"{game.title}: {lookup.error}")
        elif lookup.status == "no_match":
            self.result.no_match += 1
            self.result.fetched += 1
        else:
            self.result.fetched += 1

        if prog.status == "error":
            self.result.errors.append(f"{game.title} (RA times): {prog.error}")

    def _finalize(self) -> None:
        """Recompute derived columns once, in bulk."""
        df = repo.load_games(self.conn)
        if df.empty:
            return
        pph = compute_points_per_hour(df)
        values = {
            int(ra_id): (None if v != v else float(v))    # v != v filters NaN
            for ra_id, v in zip(df["ra_id"], pph)
        }
        repo.store_points_per_hour(self.conn, values)

    @staticmethod
    def _describe(lookup: LookupResult, prog: ProgressionResult) -> str:
        bits = []
        if lookup.beat:
            bits.append(f"HLTB {lookup.beat}h")
        if prog.ra_master:
            bits.append(f"RA master {prog.ra_master}h")
        if lookup.status == "error":
            return f"error: {lookup.error}"
        if lookup.status == "no_match":
            return "no HLTB match"
        return ", ".join(bits) if bits else "no times"
