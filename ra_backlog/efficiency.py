"""Derived backlog metrics and the session planner.

Vectorized (item #16) and NaN-correct (item #2).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Column preference order for "how long does this game take to finish".
TIME_COLUMNS = ("ra_master", "hltb_complete", "hltb_beat")


def _positive(series: pd.Series) -> pd.Series:
    """Values > 0, everything else NaN. Coerces junk to NaN rather than raising."""
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.where(numeric > 0)


def effective_hours(df: pd.DataFrame,
                    columns: tuple[str, ...] = TIME_COLUMNS) -> pd.Series:
    """Coalesce the time columns in preference order.

    Item #2. The old implementation was::

        time_val = row.get('RA_Master') or row.get('HLTB_Complete') or ...

    ``or`` treats NaN as truthy, so after any read_excel round-trip this
    returned NaN from the first column and never consulted the fallbacks --
    silently contradicting the documented behaviour. Games with HLTB data but
    no RA mastery time got no efficiency score at all.
    """
    result = pd.Series(np.nan, index=df.index, dtype="float64")
    for col in columns:
        if col in df.columns:
            result = result.fillna(_positive(df[col]))
    return result


def compute_points_per_hour(df: pd.DataFrame,
                            columns: tuple[str, ...] = TIME_COLUMNS) -> pd.Series:
    """Points per hour towards the current goal. NaN where uncomputable."""
    hours = effective_hours(df, columns)
    points = _positive(df["points"]) if "points" in df.columns else None
    if points is None:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return (points / hours).round(1)


def add_metrics(df: pd.DataFrame,
                columns: tuple[str, ...] = TIME_COLUMNS,
                earned_column: str = "earned") -> pd.DataFrame:
    """Attach derived columns. Returns a new frame; does not mutate.

    `columns` and `earned_column` come from the user's goal/mode preferences,
    so the same frame can be read as softcore-mastery or hardcore-beat.
    """
    out = df.copy()
    out["effective_hours"] = effective_hours(out, columns)
    out["points_per_hour"] = compute_points_per_hour(out, columns)

    if earned_column not in out.columns:
        earned_column = "earned"

    if "achievements" in out.columns and earned_column in out.columns:
        total = pd.to_numeric(out["achievements"], errors="coerce")
        earned = pd.to_numeric(out[earned_column], errors="coerce").fillna(0)
        out["remaining_achievements"] = (total - earned).clip(lower=0)
        pct = (earned / total.where(total > 0)).clip(upper=1.0)
        out["completion_pct"] = (pct * 100).round(1)
        # Item #19: time left, not time from scratch.
        out["remaining_hours"] = (out["effective_hours"] * (1 - pct.fillna(0))).round(1)
    return out


def summarize(df: pd.DataFrame, columns: tuple[str, ...] = TIME_COLUMNS) -> dict:
    hours = effective_hours(df, columns)
    with_hltb = df["hltb_beat"].notna() | df["hltb_complete"].notna()
    with_ra = df["ra_master"].notna()
    return {
        "total_games": int(len(df)),
        "with_hltb": int(with_hltb.sum()),
        "with_ra_mastery": int(with_ra.sum()),
        "total_hours": round(float(hours.sum(skipna=True)), 1),
        "avg_hours": round(float(hours.mean(skipna=True)), 1) if hours.notna().any() else 0.0,
        "total_points": int(pd.to_numeric(df["points"], errors="coerce").fillna(0).sum()),
        "no_time_data": int(hours.isna().sum()),
    }


# --- Planner (item #20) -----------------------------------------------------

@dataclass
class PlannedGame:
    ra_id: int
    title: str
    system: str
    points: int
    hours: float
    points_per_hour: float


@dataclass
class Plan:
    games: list[PlannedGame]
    total_hours: float
    total_points: int
    budget_hours: float

    @property
    def efficiency(self) -> float:
        return round(self.total_points / self.total_hours, 1) if self.total_hours else 0.0


def plan_session(df: pd.DataFrame, budget_hours: float,
                 granularity: float = 0.25,
                 max_games: int | None = None,
                 columns: tuple[str, ...] = TIME_COLUMNS) -> Plan:
    """Pick the games that maximise points within an hour budget.

    Exact 0/1 knapsack via DP over discretized hours. At ~350 games and a
    budget of a few hundred hours this is a few hundred thousand operations --
    fast enough that there is no reason to settle for the greedy approximation.
    """
    work = df.copy()
    work["plan_hours"] = effective_hours(work, columns)
    work["plan_points"] = pd.to_numeric(work["points"], errors="coerce")
    work = work[(work["plan_hours"] > 0) & (work["plan_points"] > 0)]
    work = work[work["plan_hours"] <= budget_hours]
    work["plan_pph"] = work["plan_points"] / work["plan_hours"]

    if work.empty or budget_hours <= 0:
        return Plan([], 0.0, 0, budget_hours)

    capacity = int(round(budget_hours / granularity))
    # Ceil, not round: understating an item's cost lets the chosen set
    # overflow the budget the user actually has.
    weights = np.maximum(1, np.ceil(work["plan_hours"].to_numpy() / granularity)).astype(int)
    values = work["plan_points"].to_numpy().astype(int)
    n = len(work)

    # best[c] = max points achievable with capacity c
    best = np.zeros(capacity + 1, dtype=np.int64)
    keep = np.zeros((n, capacity + 1), dtype=bool)

    for i in range(n):
        w, v = weights[i], values[i]
        if w > capacity:
            continue
        # Iterate downward so each item is used at most once.
        prev = best[:-w].copy() if w else best.copy()
        candidate = prev + v
        target = best[w:]
        take = candidate > target
        keep[i, w:] = take
        best[w:] = np.where(take, candidate, target)

    # Walk the decisions back out.
    chosen: list[int] = []
    c = capacity
    for i in range(n - 1, -1, -1):
        if c <= 0:
            break
        if keep[i, c]:
            chosen.append(i)
            c -= weights[i]

    picked = work.iloc[sorted(chosen)]
    if max_games is not None and len(picked) > max_games:
        picked = picked.nlargest(max_games, "plan_pph")

    games = [
        PlannedGame(
            ra_id=int(r.ra_id),
            title=str(r.title),
            system=str(r.system),
            points=int(r.plan_points),
            hours=round(float(r.plan_hours), 1),
            points_per_hour=round(float(r.plan_points / r.plan_hours), 1),
        )
        for r in picked.itertuples()
    ]
    games.sort(key=lambda g: g.points_per_hour, reverse=True)

    return Plan(
        games=games,
        total_hours=round(sum(g.hours for g in games), 1),
        total_points=sum(g.points for g in games),
        budget_hours=budget_hours,
    )
