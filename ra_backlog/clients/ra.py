"""RetroAchievements API client."""
from __future__ import annotations

from typing import Iterable, Sequence

import aiohttp

from ..config import RA_API_BASE, REQUEST_TIMEOUT, log
from ..models import Game, ProgressionResult, RAAuthError, RATransportError
from .http import get_json

# API_GetUserProgress accepts a comma-separated id list; keep batches modest.
PROGRESS_BATCH = 50


class RAClient:
    """Async client. Use as a context manager so the session is closed."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession | None = None):
        self.api_key = api_key
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "RAClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("RAClient used outside its context manager")
        return self._session

    def _url(self, endpoint: str) -> str:
        return f"{RA_API_BASE}/{endpoint}"

    async def fetch_want_to_play(self, username: str,
                                 page_size: int = 500,
                                 on_page=None) -> list[Game]:
        """Page through the user's Want to Play list.

        Item #8: a bad key raises RAAuthError rather than calling sys.exit(),
        so the interactive menu survives a typo'd API key.
        """
        games: list[Game] = []
        offset = 0

        while True:
            data = await get_json(self.session, self._url("API_GetUserWantToPlayList.php"),
                                  {"y": self.api_key, "u": username,
                                   "c": page_size, "o": offset})
            results = data.get("Results", []) or []
            total = int(data.get("Total", 0) or 0)
            if not results:
                break

            for g in results:
                if not g.get("ID"):
                    continue
                games.append(Game(
                    ra_id=int(g["ID"]),
                    title=g.get("Title", "") or "",
                    system=g.get("ConsoleName", "") or "",
                    achievements=int(g.get("AchievementsPublished", 0) or 0),
                    points=int(g.get("PointsTotal", 0) or 0),
                ))

            if on_page:
                on_page(len(games), total)

            offset += page_size
            if total and offset >= total:
                break

        return games

    async def fetch_progression(self, game_id: int) -> ProgressionResult:
        """Median completion times for one game.

        Item #4: distinguishes "no player data" from "the request failed".
        The old version funnelled both into a bare `except: pass`, and the
        caller then cached the empty result as though it were an answer.
        """
        try:
            data = await get_json(self.session, self._url("API_GetGameProgression.php"),
                                  {"y": self.api_key, "i": game_id})
        except RAAuthError:
            raise
        except RATransportError as exc:
            return ProgressionResult(status="error", error=str(exc))
        except Exception as exc:                       # pragma: no cover
            return ProgressionResult(status="error", error=f"{type(exc).__name__}: {exc}")

        if not isinstance(data, dict):
            return ProgressionResult(status="no_data")

        result = ProgressionResult(
            ra_beat=_hours(data.get("MedianTimeToBeat")),
            ra_master=_hours(data.get("MedianTimeToMaster")),
            ra_beat_hardcore=_hours(data.get("MedianTimeToBeatHardcore")),
            ra_master_hardcore=_hours(data.get("MedianTimeToMasterHardcore")),
            ra_players=_int(data.get("NumDistinctPlayers")),
        )
        if result.ra_beat is None and result.ra_master is None:
            result.status = "no_data"
        return result

    async def fetch_user_progress(self, username: str,
                                  game_ids: Sequence[int]) -> dict[int, tuple[int, int]]:
        """Achievements already earned per game (item #19).

        Returns {ra_id: (earned, earned_hardcore)}. This is what turns the
        backlog from "time to master from scratch" into "time remaining".
        """
        out: dict[int, tuple[int, int]] = {}
        ids = list(game_ids)

        for start in range(0, len(ids), PROGRESS_BATCH):
            batch = ids[start:start + PROGRESS_BATCH]
            try:
                data = await get_json(
                    self.session, self._url("API_GetUserProgress.php"),
                    {"y": self.api_key, "u": username,
                     "i": ",".join(str(i) for i in batch)},
                )
            except RAAuthError:
                raise
            except Exception as exc:
                log.debug("User progress batch failed: %s", exc)
                continue

            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                try:
                    out[int(key)] = (
                        int(value.get("NumAchieved", 0) or 0),
                        int(value.get("NumAchievedHardcore", 0) or 0),
                    )
                except (TypeError, ValueError):
                    continue

        return out


    async def fetch_achievements(self, username: str, game_id: int) -> dict:
        """Per-achievement detail for one game (items #7 and #9).

        `API_GetGameInfoAndUserProgress` returns the whole set with earn counts
        and, for the named user, which ones are already earned -- so one call
        feeds both the rarity score and the drill-down list.
        """
        try:
            data = await get_json(
                self.session, self._url("API_GetGameInfoAndUserProgress.php"),
                {"y": self.api_key, "u": username, "g": game_id, "a": 1},
            )
        except RAAuthError:
            raise
        except Exception as exc:
            log.debug("Achievement fetch failed for %s: %s", game_id, exc)
            return {"status": "error", "error": str(exc), "achievements": []}

        if not isinstance(data, dict):
            return {"status": "no_data", "achievements": []}

        raw = data.get("Achievements") or {}
        if isinstance(raw, list):                       # some responses use a list
            raw = {str(a.get("ID")): a for a in raw if isinstance(a, dict)}

        total_players = (_int(data.get("NumDistinctPlayersCasual"))
                         or _int(data.get("NumDistinctPlayers")) or 0)

        rows = []
        for key, ach in raw.items():
            if not isinstance(ach, dict):
                continue
            rows.append({
                "achievement_id": _int(ach.get("ID")) or _int(key),
                "title": ach.get("Title"),
                "description": ach.get("Description"),
                "points": _int(ach.get("Points")) or 0,
                "num_awarded": _int(ach.get("NumAwarded")),
                "num_awarded_hc": _int(ach.get("NumAwardedHardcore")),
                "total_players": total_players or None,
                "earned_at": ach.get("DateEarned"),
                "earned_hc_at": ach.get("DateEarnedHardcore"),
                "display_order": _int(ach.get("DisplayOrder")),
            })

        return {
            "status": "ok" if rows else "no_data",
            "achievements": rows,
            "total_players": total_players,
            "num_awarded_to_user": _int(data.get("NumAwardedToUser")),
            "num_awarded_to_user_hc": _int(data.get("NumAwardedToUserHardcore")),
        }

    async def fetch_rank_and_score(self, username: str) -> dict:
        """Your current points and site rank (item #8)."""
        try:
            data = await get_json(
                self.session, self._url("API_GetUserRankAndScore.php"),
                {"y": self.api_key, "u": username},
            )
        except RAAuthError:
            raise
        except Exception as exc:
            log.debug("Rank fetch failed: %s", exc)
            return {"status": "error", "error": str(exc)}

        if not isinstance(data, dict):
            return {"status": "no_data"}

        return {
            "status": "ok",
            "score": _int(data.get("Score")) or 0,
            "softcore_score": _int(data.get("SoftcoreScore")) or 0,
            "rank": _int(data.get("Rank")),
            "total_ranked": _int(data.get("TotalRanked")),
        }


def _hours(seconds) -> float | None:
    """RA reports seconds; the backlog is measured in hours."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    return round(value / 3600, 1) if value > 0 else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
