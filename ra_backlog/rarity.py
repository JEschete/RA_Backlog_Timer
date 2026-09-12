"""Achievement rarity and set difficulty.

Time alone is a poor proxy for how hard a set is. A two-hour set containing a
0.3%-earn-rate achievement is not the quick win a points-per-hour column claims
it is -- that one achievement can cost more than the other nineteen combined.
This module turns per-achievement earn rates into a difficulty signal that sits
alongside time rather than replacing it.

Pure functions; no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Earn-rate thresholds, expressed as a percentage of players who hold the set.
BRUTAL = 1.0        # sub-1%: usually a challenge run or a mastery wall
HARD = 5.0
MODERATE = 20.0

BAND_LABELS = ("brutal", "hard", "moderate", "common")


def earn_rate(num_awarded: float | None, total_players: float | None) -> float | None:
    """Percentage of set owners holding this achievement."""
    try:
        awarded = float(num_awarded)
        players = float(total_players)
    except (TypeError, ValueError):
        return None
    if players <= 0 or awarded < 0:
        return None
    return min(100.0, (awarded / players) * 100.0)


def band(rate: float | None) -> str:
    if rate is None:
        return "common"
    if rate < BRUTAL:
        return "brutal"
    if rate < HARD:
        return "hard"
    if rate < MODERATE:
        return "moderate"
    return "common"


def achievement_weight(rate: float | None) -> float:
    """How much a single achievement contributes to set difficulty.

    Rarity scales non-linearly -- the gap between a 40% and a 20% achievement is
    trivial next to the gap between 2% and 0.5% -- so weight on a log scale and
    clamp the tail so a single 0.01% outlier cannot swamp everything else.
    """
    if rate is None or rate <= 0:
        rate = 0.1
    rate = max(0.1, min(100.0, rate))
    # log10(100/rate): 100% -> 0, 10% -> 1, 1% -> 2, 0.1% -> 3
    return math.log10(100.0 / rate)


def score_set(rates: list[float | None]) -> float | None:
    """Difficulty score for a whole achievement set, roughly 0-100.

    Deliberately dominated by the hardest few achievements: a set is as hard as
    its wall, not as hard as its average.
    """
    usable = [r for r in rates if r is not None]
    if not usable:
        return None

    weights = sorted((achievement_weight(r) for r in usable), reverse=True)
    # Hardest achievement counts full, next few taper off, the long tail barely
    # registers -- 1, 1/2, 1/3, ... which converges rather than growing with set size.
    weighted = sum(w / (i + 1) for i, w in enumerate(weights))
    normaliser = sum(1 / (i + 1) for i in range(len(weights)))
    mean_weighted = weighted / normaliser if normaliser else 0.0

    # weight 3 (a 0.1% achievement) maps to 100.
    return round(min(100.0, (mean_weighted / 3.0) * 100.0), 1)


@dataclass
class SetDifficulty:
    score: float | None
    rarest_pct: float | None
    counts: dict


def analyse(rows: list[dict]) -> SetDifficulty:
    """Summarise a set from raw achievement rows.

    Each row needs `num_awarded` (or `num_awarded_hc`) and `total_players`.
    """
    rates = []
    for row in rows:
        rate = earn_rate(row.get("num_awarded"), row.get("total_players"))
        rates.append(rate)

    usable = [r for r in rates if r is not None]
    counts = {label: 0 for label in BAND_LABELS}
    for rate in usable:
        counts[band(rate)] += 1

    return SetDifficulty(
        score=score_set(rates),
        rarest_pct=round(min(usable), 2) if usable else None,
        counts=counts,
    )


def adjusted_points_per_hour(points_per_hour: pd.Series,
                             rarity_score: pd.Series,
                             strength: float = 0.5) -> pd.Series:
    """Discount efficiency by set difficulty.

    A raw points-per-hour ranking sends you at the brutal sets first, because
    dense point values cluster in exactly the sets that will stall you. This
    applies a penalty proportional to difficulty; `strength` 0 disables it and
    1 halves the score of a maximally brutal set.
    """
    pph = pd.to_numeric(points_per_hour, errors="coerce")
    score = pd.to_numeric(rarity_score, errors="coerce").fillna(0.0).clip(0, 100)
    factor = 1.0 - (score / 100.0) * float(strength)
    return (pph * factor).round(1)


def realistic_hours(effective_hours: pd.Series,
                    rarity_score: pd.Series,
                    max_multiplier: float = 2.5) -> pd.Series:
    """Inflate the time estimate for sets with a difficulty wall.

    RA medians are medians of people who *finished*; they under-report how long
    a brutal set takes someone approaching it for the first time.
    """
    hours = pd.to_numeric(effective_hours, errors="coerce")
    score = pd.to_numeric(rarity_score, errors="coerce").fillna(0.0).clip(0, 100)
    multiplier = 1.0 + (score / 100.0) * (float(max_multiplier) - 1.0)
    return (hours * multiplier).round(1)
