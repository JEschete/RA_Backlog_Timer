"""Tests for derived metrics and the planner.

The NaN cases are the regression net for item #2.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ra_backlog.efficiency import (
    add_metrics,
    compute_points_per_hour,
    effective_hours,
    plan_session,
    summarize,
)


def frame(rows):
    cols = ["ra_id", "title", "system", "points", "achievements",
            "ra_master", "hltb_complete", "hltb_beat", "earned"]
    return pd.DataFrame(rows, columns=cols)


class TestEffectiveHoursFallback:
    """Item #2: NaN is truthy, so `a or b` never reached the fallbacks."""

    def test_falls_back_to_hltb_complete_when_ra_master_is_nan(self):
        df = frame([[1, "X", "SNES", 400, 50, np.nan, 12.0, 8.0, 0]])
        assert effective_hours(df).iloc[0] == 12.0

    def test_falls_back_to_hltb_beat_when_both_higher_are_nan(self):
        df = frame([[1, "X", "SNES", 400, 50, np.nan, np.nan, 8.0, 0]])
        assert effective_hours(df).iloc[0] == 8.0

    def test_prefers_ra_master_when_present(self):
        df = frame([[1, "X", "SNES", 400, 50, 20.0, 12.0, 8.0, 0]])
        assert effective_hours(df).iloc[0] == 20.0

    def test_zero_is_treated_as_missing(self):
        df = frame([[1, "X", "SNES", 400, 50, 0.0, 12.0, 8.0, 0]])
        assert effective_hours(df).iloc[0] == 12.0

    def test_all_missing_yields_nan(self):
        df = frame([[1, "X", "SNES", 400, 50, np.nan, np.nan, np.nan, 0]])
        assert pd.isna(effective_hours(df).iloc[0])

    def test_points_per_hour_uses_the_fallback(self):
        """The exact scenario that silently produced None before."""
        df = frame([[1, "X", "SNES", 400, 50, np.nan, 12.0, 8.0, 0]])
        assert compute_points_per_hour(df).iloc[0] == pytest.approx(33.3)

    def test_survives_an_excel_round_trip(self, tmp_path):
        """read_excel turns None into NaN -- the trigger for the original bug."""
        df = frame([[1, "X", "SNES", 400, 50, None, 12.0, 8.0, 0]])
        path = tmp_path / "rt.xlsx"
        df.to_excel(path, index=False)
        reloaded = pd.read_excel(path)
        assert compute_points_per_hour(reloaded).iloc[0] == pytest.approx(33.3)


class TestAddMetrics:
    def test_remaining_reflects_earned_achievements(self):
        df = frame([[1, "X", "SNES", 400, 100, 20.0, np.nan, np.nan, 25]])
        out = add_metrics(df)
        assert out["remaining_achievements"].iloc[0] == 75
        assert out["completion_pct"].iloc[0] == pytest.approx(25.0)
        assert out["remaining_hours"].iloc[0] == pytest.approx(15.0)

    def test_does_not_mutate_input(self):
        df = frame([[1, "X", "SNES", 400, 50, 20.0, np.nan, np.nan, 0]])
        before = set(df.columns)
        add_metrics(df)
        assert set(df.columns) == before


class TestSummarize:
    def test_counts(self):
        df = frame([
            [1, "A", "SNES", 400, 50, 20.0, np.nan, np.nan, 0],
            [2, "B", "NES", 200, 30, np.nan, 10.0, np.nan, 0],
            [3, "C", "GBA", 100, 20, np.nan, np.nan, np.nan, 0],
        ])
        s = summarize(df)
        assert s["total_games"] == 3
        assert s["with_ra_mastery"] == 1
        assert s["total_hours"] == pytest.approx(30.0)
        assert s["no_time_data"] == 1


class TestPlanner:
    def test_respects_the_budget(self):
        df = frame([
            [1, "A", "SNES", 400, 50, 5.0, np.nan, np.nan, 0],
            [2, "B", "NES", 300, 30, 4.0, np.nan, np.nan, 0],
            [3, "C", "GBA", 100, 20, 9.0, np.nan, np.nan, 0],
        ])
        plan = plan_session(df, budget_hours=10.0)
        assert plan.total_hours <= 10.0

    def test_finds_the_optimum_not_just_the_greedy_pick(self):
        """Greedy by ratio takes A (80/h) then stalls; A+B is worth more."""
        df = frame([
            [1, "A", "SNES", 800, 50, 10.0, np.nan, np.nan, 0],   # 80/h
            [2, "B", "NES", 540, 30, 9.0, np.nan, np.nan, 0],     # 60/h
            [3, "C", "GBA", 950, 20, 19.0, np.nan, np.nan, 0],    # 50/h
        ])
        plan = plan_session(df, budget_hours=19.0)
        assert plan.total_points == 1340          # A + B
        assert {g.title for g in plan.games} == {"A", "B"}

    def test_skips_games_that_do_not_fit(self):
        df = frame([[1, "Huge", "PS2", 9999, 50, 500.0, np.nan, np.nan, 0]])
        assert plan_session(df, budget_hours=10.0).games == []

    def test_uses_the_hltb_fallback_too(self):
        """A planner built on the old coalesce would see no candidates here."""
        df = frame([[1, "A", "SNES", 400, 50, np.nan, 6.0, np.nan, 0]])
        plan = plan_session(df, budget_hours=10.0)
        assert len(plan.games) == 1

    def test_empty_input(self):
        assert plan_session(frame([]), budget_hours=10.0).games == []

    @pytest.mark.parametrize("budget", [5.0, 10.0, 20.0, 37.5, 50.0])
    def test_never_exceeds_the_budget(self, budget):
        """Discretization must not let the chosen set overflow the budget."""
        rng = np.random.default_rng(0)
        rows = [[i, f"G{i}", "SNES", int(rng.integers(50, 800)), 50,
                 float(round(rng.uniform(0.7, 14.3), 1)), np.nan, np.nan, 0]
                for i in range(60)]
        plan = plan_session(frame(rows), budget_hours=budget)
        assert plan.total_hours <= budget
