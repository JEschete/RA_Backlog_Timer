"""Plain data carriers shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


class RAAuthError(RuntimeError):
    """Raised on a 401 from RetroAchievements.

    Item #8: the old code called sys.exit() from inside a fetch helper, which
    killed the whole process from under the interactive menu. Callers catch
    this instead and decide what to do.
    """


class RATransportError(RuntimeError):
    """A network/HTTP failure that is not an auth problem."""


@dataclass
class Game:
    ra_id: int
    title: str
    system: str = ""
    achievements: int = 0
    points: int = 0

    hltb_beat: float | None = None
    hltb_complete: float | None = None
    hltb_name: str | None = None
    match_quality: str | None = None       # exact|fuzzy|loose|poor|none
    match_similarity: float | None = None

    ra_beat: float | None = None
    ra_master: float | None = None
    ra_beat_hardcore: float | None = None      # item #11
    ra_master_hardcore: float | None = None    # item #11
    ra_players: int | None = None

    earned: int | None = None                  # item #19
    earned_hardcore: int | None = None

    points_per_hour: float | None = None
    in_want_to_play: bool = True

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["in_want_to_play"] = int(self.in_want_to_play)
        return d


@dataclass
class LookupResult:
    """Outcome of one HLTB search. `status` drives cache retention (item #3)."""
    status: str = "ok"                 # ok | no_match | error
    beat: float | None = None
    complete: float | None = None
    hltb_name: str | None = None
    similarity: float = 0.0
    quality: str | None = None         # exact|fuzzy|loose|poor|none
    comment: str | None = None
    error: str | None = None

    @property
    def is_cacheable_forever(self) -> bool:
        return self.status in ("ok", "no_match")


@dataclass
class ProgressionResult:
    ra_beat: float | None = None
    ra_master: float | None = None
    ra_beat_hardcore: float | None = None
    ra_master_hardcore: float | None = None
    ra_players: int | None = None
    status: str = "ok"                 # ok | no_data | error
    error: str | None = None


@dataclass
class ScanProgress:
    """One SSE frame."""
    done: int = 0
    total: int = 0
    title: str = ""
    detail: str = ""
    state: str = "running"             # running | done | error | cancelled
    errors: list[str] = field(default_factory=list)

    def to_event(self) -> dict[str, Any]:
        return asdict(self)
