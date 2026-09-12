"""User preferences that change what every metric means.

The tool used to hardcode "softcore mastery" as the goal. For someone who only
wants to finish games, or who plays exclusively hardcore, every number on
screen was answering the wrong question. These settings repoint the metric
column at the right source everywhere at once.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict

from .storage import repo

# Goal: are you trying to beat games, or master them?
GOAL_BEAT = "beat"
GOAL_MASTER = "master"
GOALS = (GOAL_BEAT, GOAL_MASTER)

# Mode: softcore or hardcore (no save states / rewind).
MODE_SOFTCORE = "softcore"
MODE_HARDCORE = "hardcore"
MODES = (MODE_SOFTCORE, MODE_HARDCORE)

# (goal, mode) -> the games column that holds that time, best first.
_TIME_COLUMNS = {
    (GOAL_MASTER, MODE_SOFTCORE): ("ra_master", "hltb_complete", "hltb_beat"),
    (GOAL_MASTER, MODE_HARDCORE): ("ra_master_hardcore", "ra_master", "hltb_complete", "hltb_beat"),
    (GOAL_BEAT, MODE_SOFTCORE): ("ra_beat", "hltb_beat", "hltb_complete"),
    (GOAL_BEAT, MODE_HARDCORE): ("ra_beat_hardcore", "ra_beat", "hltb_beat", "hltb_complete"),
}

_META_GOAL = "pref_goal"
_META_MODE = "pref_mode"
_META_WEEKLY_HOURS = "pref_weekly_hours"
_META_CALIBRATION = "pref_use_calibration"


@dataclass(frozen=True)
class Preferences:
    goal: str = GOAL_MASTER
    mode: str = MODE_SOFTCORE
    weekly_hours: float = 8.0
    use_calibration: bool = True

    @property
    def time_columns(self) -> tuple[str, ...]:
        """Coalesce order for "how long does this take", given the settings.

        Hardcore falls back to the softcore column rather than to nothing:
        a hardcore median is often missing where a softcore one exists, and a
        slightly-wrong number beats an empty cell.
        """
        return _TIME_COLUMNS[(self.goal, self.mode)]

    @property
    def goal_label(self) -> str:
        """What RetroAchievements actually calls this goal.

        On RA, earning every achievement in *softcore* is a **completion**;
        **mastery** specifically means doing it in hardcore. Calling the
        softcore goal "master" is simply the wrong word.
        """
        if self.goal == GOAL_BEAT:
            return "beat"
        return "master" if self.mode == MODE_HARDCORE else "complete"

    @property
    def label(self) -> str:
        return f"{self.mode} {self.goal_label}"

    @property
    def earned_column(self) -> str:
        return "earned_hardcore" if self.mode == MODE_HARDCORE else "earned"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time_columns"] = list(self.time_columns)
        d["label"] = self.label
        return d


def load(conn: sqlite3.Connection) -> Preferences:
    goal = repo.get_meta(conn, _META_GOAL, GOAL_MASTER)
    mode = repo.get_meta(conn, _META_MODE, MODE_SOFTCORE)
    weekly = repo.get_meta(conn, _META_WEEKLY_HOURS, "8")
    calib = repo.get_meta(conn, _META_CALIBRATION, "1")

    return Preferences(
        goal=goal if goal in GOALS else GOAL_MASTER,
        mode=mode if mode in MODES else MODE_SOFTCORE,
        weekly_hours=_float(weekly, 8.0),
        use_calibration=calib == "1",
    )


def save(conn: sqlite3.Connection, prefs: Preferences) -> None:
    repo.set_meta(conn, _META_GOAL, prefs.goal)
    repo.set_meta(conn, _META_MODE, prefs.mode)
    repo.set_meta(conn, _META_WEEKLY_HOURS, str(prefs.weekly_hours))
    repo.set_meta(conn, _META_CALIBRATION, "1" if prefs.use_calibration else "0")


def update(conn: sqlite3.Connection, **changes) -> Preferences:
    """Apply a partial update, ignoring unknown or invalid values."""
    current = load(conn)
    goal = changes.get("goal", current.goal)
    mode = changes.get("mode", current.mode)
    prefs = Preferences(
        goal=goal if goal in GOALS else current.goal,
        mode=mode if mode in MODES else current.mode,
        weekly_hours=_float(changes.get("weekly_hours", current.weekly_hours),
                            current.weekly_hours),
        use_calibration=bool(changes.get("use_calibration", current.use_calibration)),
    )
    save(conn, prefs)
    return prefs


def _float(value, fallback: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback
    return f if f > 0 else fallback
