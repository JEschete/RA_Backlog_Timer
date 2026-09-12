"""Selection strategies over the backlog: roulette, rank targets, schedules."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .efficiency import TIME_COLUMNS, effective_hours


# --- Roulette ---------------------------------------------------------------

@dataclass
class RouletteResult:
    game: dict | None
    candidates: int
    constraints: dict


def roulette(df: pd.DataFrame, *,
             max_hours: float | None = None,
             min_hours: float | None = None,
             systems: list[str] | None = None,
             max_rarity: float | None = None,
             min_points_per_hour: float | None = None,
             require_time_data: bool = True,
             seed: int | None = None,
             columns: tuple[str, ...] = TIME_COLUMNS) -> RouletteResult:
    """Pick one game at random from everything matching the constraints.

    Weighted towards efficiency rather than uniform: a flat random pick over a
    350-game backlog mostly returns things you were never going to play, which
    is why the feature usually gets used twice and abandoned.
    """
    work = df.copy()
    work["roulette_hours"] = effective_hours(work, columns)

    constraints = {
        "max_hours": max_hours, "min_hours": min_hours,
        "systems": systems, "max_rarity": max_rarity,
        "min_points_per_hour": min_points_per_hour,
    }

    if require_time_data:
        work = work[work["roulette_hours"].notna()]
    if max_hours is not None:
        work = work[work["roulette_hours"] <= max_hours]
    if min_hours is not None:
        work = work[work["roulette_hours"] >= min_hours]
    if systems:
        work = work[work["system"].isin(systems)]
    if max_rarity is not None and "rarity_score" in work.columns:
        score = pd.to_numeric(work["rarity_score"], errors="coerce").fillna(0)
        work = work[score <= max_rarity]
    if min_points_per_hour is not None and "points_per_hour" in work.columns:
        pph = pd.to_numeric(work["points_per_hour"], errors="coerce").fillna(0)
        work = work[pph >= min_points_per_hour]
    if "hidden" in work.columns:
        work = work[work["hidden"].fillna(0).astype(int) == 0]

    if work.empty:
        return RouletteResult(None, 0, constraints)

    rng = random.Random(seed)
    weights = pd.to_numeric(work.get("points_per_hour"), errors="coerce")
    if weights is None or weights.notna().sum() == 0:
        pick = work.iloc[rng.randrange(len(work))]
    else:
        # Floor at a small positive weight so nothing is unpickable.
        w = weights.fillna(weights.median() if weights.notna().any() else 1.0)
        w = w.clip(lower=0.1).to_numpy(dtype=float)
        idx = rng.choices(range(len(work)), weights=w, k=1)[0]
        pick = work.iloc[idx]

    return RouletteResult(_row_to_dict(pick), len(work), constraints)


# --- Points target (rank planner) -------------------------------------------

@dataclass
class TargetPlan:
    games: list = field(default_factory=list)
    total_points: int = 0
    total_hours: float = 0.0
    target_points: int = 0
    reachable: bool = True


def plan_for_points(df: pd.DataFrame, target_points: int,
                    granularity: float = 0.25,
                    columns: tuple[str, ...] = TIME_COLUMNS) -> TargetPlan:
    """Cheapest set of games (in hours) worth at least `target_points`.

    The inverse of the session planner: there the budget is time and the prize
    is points; here the target is points and the cost is time. Used for
    "how do I reach the next rank fastest?".
    """
    work = df.copy()
    work["tp_hours"] = effective_hours(work, columns)
    work["tp_points"] = pd.to_numeric(work["points"], errors="coerce")
    work = work[(work["tp_hours"] > 0) & (work["tp_points"] > 0)]

    if target_points <= 0 or work.empty:
        return TargetPlan(target_points=max(0, target_points),
                          reachable=target_points <= 0)

    available = int(work["tp_points"].sum())
    if available < target_points:
        # Nothing can reach it; hand back everything so the shortfall is visible.
        rows = [_row_to_dict(r) for _, r in work.iterrows()]
        return TargetPlan(games=rows, total_points=available,
                          total_hours=round(float(work["tp_hours"].sum()), 1),
                          target_points=target_points, reachable=False)

    # cost[p] = minimum weighted hours to earn at least p points
    weights = np.maximum(1, np.ceil(work["tp_hours"].to_numpy() / granularity)).astype(int)
    values = work["tp_points"].to_numpy().astype(int)
    n = len(work)

    INF = np.iinfo(np.int64).max // 4
    cost = np.full(target_points + 1, INF, dtype=np.int64)
    cost[0] = 0
    keep = np.zeros((n, target_points + 1), dtype=bool)

    for i in range(n):
        v, w = int(values[i]), int(weights[i])
        # Points beyond the target are as good as exactly the target.
        shifted = np.empty_like(cost)
        shifted[:] = INF
        src = cost[: target_points + 1 - v] if v <= target_points else cost[:1]
        if v <= target_points:
            shifted[v:] = src + w
            shifted[:v] = cost[0] + w      # any item alone covers small targets
        else:
            shifted[:] = cost[0] + w
        take = shifted < cost
        keep[i] = take
        cost = np.where(take, shifted, cost)

    chosen: list[int] = []
    p = target_points
    for i in range(n - 1, -1, -1):
        if p <= 0:
            break
        if keep[i, p]:
            chosen.append(i)
            p = max(0, p - int(values[i]))

    picked = work.iloc[sorted(chosen)]
    rows = [_row_to_dict(r) for _, r in picked.iterrows()]
    rows.sort(key=lambda g: g.get("points_per_hour") or 0, reverse=True)

    return TargetPlan(
        games=rows,
        total_points=int(picked["tp_points"].sum()),
        total_hours=round(float(picked["tp_hours"].sum()), 1),
        target_points=target_points,
        reachable=True,
    )


# --- Multi-week schedule ----------------------------------------------------

@dataclass
class ScheduleWeek:
    week_start: str
    games: list = field(default_factory=list)
    hours: float = 0.0
    points: int = 0


@dataclass
class Schedule:
    weeks: list = field(default_factory=list)
    weekly_hours: float = 0.0
    total_hours: float = 0.0
    total_points: int = 0
    unscheduled: int = 0


def build_schedule(df: pd.DataFrame, *,
                   weekly_hours: float,
                   weeks: int = 12,
                   start: date | None = None,
                   columns: tuple[str, ...] = TIME_COLUMNS,
                   allow_split: bool = True) -> Schedule:
    """Lay games across a run of weeks at a sustainable pace.

    "What can I do in 20 hours" is a basket; this is a calendar. Games longer
    than a single week's budget are split across consecutive weeks rather than
    being silently dropped, which is what makes the long tail schedulable at all.
    """
    work = df.copy()
    work["sched_hours"] = effective_hours(work, columns)
    work["sched_points"] = pd.to_numeric(work["points"], errors="coerce").fillna(0)
    work = work[work["sched_hours"] > 0]

    if "hidden" in work.columns:
        work = work[work["hidden"].fillna(0).astype(int) == 0]

    # Pinned games first, then by efficiency.
    if "pinned" in work.columns:
        work["sched_pin"] = work["pinned"].fillna(0).astype(int)
    else:
        work["sched_pin"] = 0
    work["sched_pph"] = work["sched_points"] / work["sched_hours"]
    work = work.sort_values(["sched_pin", "sched_pph"], ascending=[False, False])

    monday = start or _next_monday()
    plan = Schedule(weekly_hours=weekly_hours)
    remaining = [(idx, float(r.sched_hours), int(r.sched_points), r)
                 for idx, r in work.iterrows()]
    cursor = 0

    for w in range(weeks):
        week = ScheduleWeek(week_start=(monday + timedelta(weeks=w)).isoformat())
        budget = weekly_hours

        while cursor < len(remaining) and budget > 0.01:
            idx, hours, points, row = remaining[cursor]

            if hours <= budget:
                week.games.append(_schedule_entry(row, hours, points, partial=False))
                week.hours += hours
                week.points += points
                budget -= hours
                cursor += 1
            elif allow_split and budget >= 0.5:
                # Take a slice now, carry the rest into next week.
                week.games.append(_schedule_entry(row, budget, 0, partial=True))
                week.hours += budget
                remaining[cursor] = (idx, hours - budget, points, row)
                budget = 0
            else:
                break

        week.hours = round(week.hours, 1)
        plan.weeks.append(week)
        if cursor >= len(remaining):
            break

    plan.total_hours = round(sum(w.hours for w in plan.weeks), 1)
    plan.total_points = sum(w.points for w in plan.weeks)
    plan.unscheduled = max(0, len(remaining) - cursor)
    return plan


def _schedule_entry(row, hours: float, points: int, partial: bool) -> dict:
    return {
        "ra_id": int(row.ra_id),
        "title": str(row.title),
        "system": str(getattr(row, "system", "")),
        "hours": round(float(hours), 1),
        "points": int(points),
        "partial": partial,
    }


def _next_monday(today: date | None = None) -> date:
    """The coming Monday, or today if today is one."""
    today = today or date.today()
    return today + timedelta(days=(0 - today.weekday()) % 7)


def _row_to_dict(row) -> dict:
    out = {}
    for key in ("ra_id", "title", "system", "points", "points_per_hour",
                "rarity_score", "achievements", "earned"):
        if key in row.index:
            value = row[key]
            if pd.isna(value):
                out[key] = None
            elif key in ("ra_id", "points", "achievements", "earned"):
                out[key] = int(value)
            else:
                out[key] = float(value) if isinstance(value, (int, float, np.number)) else str(value)
    for key in ("roulette_hours", "tp_hours", "sched_hours"):
        if key in row.index and not pd.isna(row[key]):
            out["hours"] = round(float(row[key]), 1)
            break
    return out
