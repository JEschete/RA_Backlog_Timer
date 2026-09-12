"""Learn how your real pace compares to RetroAchievements medians.

RA medians describe the median player who *finished the set*. You are not that
person, and the direction of the error is consistent per person: some people
routinely run 1.4x the median, others 0.7x. Once enough sessions are logged,
every estimate on the page can be corrected by a factor derived from your own
history instead of trusting a stranger's median forever.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Below this many completed games the sample is too small to trust.
MIN_SAMPLES = 3
# Never scale an estimate by more than this in either direction; beyond it the
# likely explanation is bad logging, not a genuinely different pace.
MIN_FACTOR = 0.4
MAX_FACTOR = 3.0


@dataclass
class Calibration:
    factor: float = 1.0
    samples: int = 0
    confident: bool = False
    median_ratio: float | None = None
    logged_hours: float = 0.0

    @property
    def description(self) -> str:
        if not self.confident:
            need = max(0, MIN_SAMPLES - self.samples)
            return f"Not enough data yet - log {need} more finished game(s)"
        if self.factor > 1.05:
            return f"You run about {self.factor:.2f}x the RA median"
        if self.factor < 0.95:
            return f"You run about {self.factor:.2f}x the RA median (faster)"
        return "Your pace matches the RA median"

    def apply(self, hours: pd.Series) -> pd.Series:
        numeric = pd.to_numeric(hours, errors="coerce")
        return (numeric * self.factor).round(1)


def compute(observations: list[dict]) -> Calibration:
    """Derive a personal pace factor from logged sessions.

    Each observation needs `logged_hours` and `estimated_hours`. Only games you
    actually finished should be passed in -- a half-played game logs fewer hours
    than the estimate for reasons that have nothing to do with your pace, and
    including them biases the factor downwards.
    """
    ratios: list[float] = []
    total_logged = 0.0

    for obs in observations:
        logged = _positive(obs.get("logged_hours"))
        estimated = _positive(obs.get("estimated_hours"))
        if logged is None:
            continue
        total_logged += logged
        if estimated is None:
            continue
        ratios.append(logged / estimated)

    if not ratios:
        return Calibration(samples=0, logged_hours=round(total_logged, 1))

    series = pd.Series(ratios)
    # Median, not mean: one abandoned 40-hour RPG should not redefine your pace.
    median_ratio = float(series.median())
    factor = max(MIN_FACTOR, min(MAX_FACTOR, median_ratio))

    return Calibration(
        factor=round(factor, 3),
        samples=len(ratios),
        confident=len(ratios) >= MIN_SAMPLES,
        median_ratio=round(median_ratio, 3),
        logged_hours=round(total_logged, 1),
    )


def velocity(sessions: list[dict], weeks: int = 8) -> dict:
    """Recent hours-per-week and the runway it implies.

    Answers "at the rate I actually play, how long is this backlog?" -- which is
    usually a more sobering number than the raw total.
    """
    if not sessions:
        return {"hours_per_week": 0.0, "weeks_sampled": 0, "total_hours": 0.0}

    df = pd.DataFrame(sessions)
    if "started_at" not in df.columns or "minutes" not in df.columns:
        return {"hours_per_week": 0.0, "weeks_sampled": 0, "total_hours": 0.0}

    df["started_at"] = pd.to_datetime(df["started_at"], errors="coerce", utc=True)
    df["minutes"] = pd.to_numeric(df["minutes"], errors="coerce").fillna(0)
    df = df.dropna(subset=["started_at"])
    if df.empty:
        return {"hours_per_week": 0.0, "weeks_sampled": 0, "total_hours": 0.0}

    cutoff = df["started_at"].max() - pd.Timedelta(weeks=weeks)
    recent = df[df["started_at"] >= cutoff]
    total_hours = float(recent["minutes"].sum()) / 60.0

    span_days = (recent["started_at"].max() - recent["started_at"].min()).days + 1
    weeks_sampled = max(1.0, span_days / 7.0)

    return {
        "hours_per_week": round(total_hours / weeks_sampled, 1),
        "weeks_sampled": round(weeks_sampled, 1),
        "total_hours": round(total_hours, 1),
    }


def project_completion(remaining_hours: float, hours_per_week: float) -> dict:
    """Turn a backlog size into a date, given an observed pace."""
    if hours_per_week <= 0 or remaining_hours <= 0:
        return {"weeks": None, "years": None, "feasible": False}
    weeks = remaining_hours / hours_per_week
    return {
        "weeks": round(weeks, 1),
        "years": round(weeks / 52.0, 1),
        "feasible": True,
    }


def _positive(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
