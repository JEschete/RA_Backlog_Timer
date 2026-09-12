"""Tests for the preference, rarity, planning and calibration logic."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ra_backlog import calibration, planning, rarity
from ra_backlog.preferences import (
    GOAL_BEAT, GOAL_MASTER, MODE_HARDCORE, MODE_SOFTCORE, Preferences,
)
from ra_backlog.storage.db import connect
from ra_backlog import preferences as prefs_module


COLS = ["ra_id", "title", "system", "points", "achievements", "earned",
        "ra_master", "ra_master_hardcore", "ra_beat", "ra_beat_hardcore",
        "hltb_complete", "hltb_beat", "points_per_hour", "rarity_score"]


def frame(rows):
    return pd.DataFrame(rows, columns=COLS)


def game(ra_id=1, title="Contra", points=400, master=20.0, master_hc=26.0,
         beat=5.0, beat_hc=7.0, pph=20.0, rarity_score=np.nan):
    return [ra_id, title, "NES", points, 50, 0, master, master_hc,
            beat, beat_hc, np.nan, np.nan, pph, rarity_score]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "prefs.db")
    yield c
    c.close()


# --- Preferences (#1, #2) ---------------------------------------------------

class TestPreferences:
    def test_goal_and_mode_pick_the_right_column(self):
        assert Preferences(GOAL_MASTER, MODE_SOFTCORE).time_columns[0] == "ra_master"
        assert Preferences(GOAL_MASTER, MODE_HARDCORE).time_columns[0] == "ra_master_hardcore"
        assert Preferences(GOAL_BEAT, MODE_SOFTCORE).time_columns[0] == "ra_beat"
        assert Preferences(GOAL_BEAT, MODE_HARDCORE).time_columns[0] == "ra_beat_hardcore"

    def test_hardcore_falls_back_to_softcore(self):
        """A missing hardcore median should not blank the row."""
        cols = Preferences(GOAL_MASTER, MODE_HARDCORE).time_columns
        assert "ra_master" in cols
        assert cols.index("ra_master_hardcore") < cols.index("ra_master")

    def test_earned_column_follows_mode(self):
        assert Preferences(mode=MODE_HARDCORE).earned_column == "earned_hardcore"
        assert Preferences(mode=MODE_SOFTCORE).earned_column == "earned"

    def test_round_trip(self, conn):
        prefs_module.save(conn, Preferences(GOAL_BEAT, MODE_HARDCORE, 12.5, False))
        loaded = prefs_module.load(conn)
        assert (loaded.goal, loaded.mode) == (GOAL_BEAT, MODE_HARDCORE)
        assert loaded.weekly_hours == 12.5
        assert loaded.use_calibration is False

    def test_defaults_when_unset(self, conn):
        p = prefs_module.load(conn)
        assert (p.goal, p.mode) == (GOAL_MASTER, MODE_SOFTCORE)

    def test_invalid_values_fall_back(self, conn):
        p = prefs_module.update(conn, goal="nonsense", mode="also-nonsense")
        assert (p.goal, p.mode) == (GOAL_MASTER, MODE_SOFTCORE)

    def test_switching_goal_changes_the_metric(self):
        from ra_backlog.efficiency import compute_points_per_hour
        df = frame([game(points=400, master=20.0, beat=5.0)])
        master = compute_points_per_hour(df, Preferences(GOAL_MASTER).time_columns)
        beat = compute_points_per_hour(df, Preferences(GOAL_BEAT).time_columns)
        assert master.iloc[0] == pytest.approx(20.0)    # 400 / 20h
        assert beat.iloc[0] == pytest.approx(80.0)      # 400 / 5h


# --- Rarity (#7) ------------------------------------------------------------

class TestRarity:
    def test_earn_rate(self):
        assert rarity.earn_rate(50, 200) == pytest.approx(25.0)
        assert rarity.earn_rate(0, 200) == 0.0
        assert rarity.earn_rate(5, 0) is None
        assert rarity.earn_rate(None, 200) is None

    @pytest.mark.parametrize("rate,expected", [
        (0.3, "brutal"), (0.99, "brutal"), (3.0, "hard"),
        (12.0, "moderate"), (55.0, "common"),
    ])
    def test_bands(self, rate, expected):
        assert rarity.band(rate) == expected

    def test_weight_increases_as_rarity_increases(self):
        assert (rarity.achievement_weight(50)
                < rarity.achievement_weight(5)
                < rarity.achievement_weight(0.5))

    def test_a_single_brutal_achievement_dominates(self):
        """A set is as hard as its wall, not as hard as its average."""
        easy = rarity.score_set([60.0] * 20)
        with_wall = rarity.score_set([60.0] * 19 + [0.4])
        assert with_wall > easy * 2

    def test_score_is_bounded(self):
        assert 0 <= rarity.score_set([0.1] * 10) <= 100
        assert 0 <= rarity.score_set([99.0] * 10) <= 100

    def test_no_data_gives_none(self):
        assert rarity.score_set([]) is None
        assert rarity.score_set([None, None]) is None

    def test_analyse_counts_bands(self):
        rows = [
            {"num_awarded": 1, "total_players": 1000},     # 0.1% brutal
            {"num_awarded": 30, "total_players": 1000},    # 3%   hard
            {"num_awarded": 900, "total_players": 1000},   # 90%  common
        ]
        result = rarity.analyse(rows)
        assert result.counts["brutal"] == 1
        assert result.counts["hard"] == 1
        assert result.counts["common"] == 1
        assert result.rarest_pct == pytest.approx(0.1)

    def test_adjusted_pph_penalises_brutal_sets(self):
        pph = pd.Series([100.0, 100.0])
        score = pd.Series([0.0, 100.0])
        adjusted = rarity.adjusted_points_per_hour(pph, score, strength=0.5)
        assert adjusted.iloc[0] == pytest.approx(100.0)
        assert adjusted.iloc[1] == pytest.approx(50.0)

    def test_realistic_hours_inflates_hard_sets(self):
        hours = pd.Series([10.0, 10.0])
        score = pd.Series([0.0, 100.0])
        out = rarity.realistic_hours(hours, score, max_multiplier=2.5)
        assert out.iloc[0] == pytest.approx(10.0)
        assert out.iloc[1] == pytest.approx(25.0)


# --- Roulette (#6) ----------------------------------------------------------

class TestRoulette:
    def test_respects_max_hours(self):
        df = frame([game(1, "Short", master=2.0), game(2, "Long", master=90.0)])
        for seed in range(15):
            result = planning.roulette(df, max_hours=5.0, seed=seed)
            assert result.game["title"] == "Short"

    def test_reports_candidate_count(self):
        df = frame([game(1, "A", master=2.0), game(2, "B", master=3.0),
                    game(3, "C", master=90.0)])
        assert planning.roulette(df, max_hours=5.0, seed=1).candidates == 2

    def test_no_candidates_returns_none(self):
        df = frame([game(1, "Long", master=90.0)])
        result = planning.roulette(df, max_hours=1.0, seed=1)
        assert result.game is None and result.candidates == 0

    def test_is_deterministic_for_a_seed(self):
        df = frame([game(i, f"G{i}", master=float(i + 1)) for i in range(1, 12)])
        a = planning.roulette(df, seed=42).game
        b = planning.roulette(df, seed=42).game
        assert a == b

    def test_skips_games_with_no_time_data(self):
        df = frame([game(1, "Unknown", master=np.nan, master_hc=np.nan,
                         beat=np.nan, beat_hc=np.nan)])
        assert planning.roulette(df, seed=1).game is None


# --- Points target (#8) -----------------------------------------------------

class TestPointsTarget:
    def test_reaches_the_target(self):
        df = frame([game(1, "A", points=500, master=5.0),
                    game(2, "B", points=300, master=2.0),
                    game(3, "C", points=200, master=1.0)])
        plan = planning.plan_for_points(df, 500)
        assert plan.reachable
        assert plan.total_points >= 500

    def test_prefers_the_cheaper_route(self):
        """B+C cost 3h for 500 points; A costs 5h for the same."""
        df = frame([game(1, "A", points=500, master=5.0),
                    game(2, "B", points=300, master=2.0),
                    game(3, "C", points=200, master=1.0)])
        plan = planning.plan_for_points(df, 500)
        assert plan.total_hours <= 3.0

    def test_unreachable_target_is_flagged(self):
        df = frame([game(1, "A", points=100, master=5.0)])
        plan = planning.plan_for_points(df, 10_000)
        assert plan.reachable is False
        assert plan.total_points == 100

    def test_zero_target(self):
        df = frame([game(1, "A", points=100, master=5.0)])
        assert planning.plan_for_points(df, 0).games == []


# --- Schedule (#12) ---------------------------------------------------------

class TestSchedule:
    def test_weeks_respect_the_budget(self):
        df = frame([game(i, f"G{i}", points=100, master=3.0) for i in range(1, 21)])
        plan = planning.build_schedule(df, weekly_hours=10.0, weeks=4)
        assert len(plan.weeks) <= 4
        for week in plan.weeks:
            assert week.hours <= 10.0 + 1e-6

    def test_long_games_are_split_not_dropped(self):
        """A 25-hour game must still be schedulable on an 8-hour week."""
        df = frame([game(1, "Epic", points=1000, master=25.0)])
        plan = planning.build_schedule(df, weekly_hours=8.0, weeks=6)
        scheduled = [g for w in plan.weeks for g in w.games]
        assert len(scheduled) > 1
        assert any(g["partial"] for g in scheduled)
        assert sum(g["hours"] for g in scheduled) == pytest.approx(25.0, abs=0.2)

    def test_weeks_start_on_mondays(self):
        # Enough work to actually span three weeks at 10h/week.
        df = frame([game(i, f"G{i}", points=100, master=5.0) for i in range(1, 13)])
        plan = planning.build_schedule(df, weekly_hours=10.0, weeks=3,
                                       start=date(2026, 8, 24))   # a Monday
        assert [w.week_start for w in plan.weeks] == [
            "2026-08-24", "2026-08-31", "2026-09-07"]

    def test_stops_early_when_the_backlog_runs_out(self):
        """No empty trailing weeks -- a short backlog yields a short schedule."""
        df = frame([game(1, "A", master=2.0)])
        plan = planning.build_schedule(df, weekly_hours=10.0, weeks=12)
        assert len(plan.weeks) == 1

    def test_empty_backlog(self):
        plan = planning.build_schedule(frame([]), weekly_hours=10.0, weeks=4)
        assert plan.total_hours == 0.0


# --- Calibration (#13) ------------------------------------------------------

class TestCalibration:
    def test_no_data_is_neutral(self):
        c = calibration.compute([])
        assert c.factor == 1.0 and not c.confident

    def test_detects_a_slower_player(self):
        obs = [{"logged_hours": 30, "estimated_hours": 20}] * 4
        c = calibration.compute(obs)
        assert c.confident and c.factor == pytest.approx(1.5)

    def test_detects_a_faster_player(self):
        obs = [{"logged_hours": 10, "estimated_hours": 20}] * 4
        c = calibration.compute(obs)
        assert c.factor == pytest.approx(0.5)

    def test_uses_median_so_one_outlier_does_not_dominate(self):
        obs = [{"logged_hours": 20, "estimated_hours": 20}] * 5
        obs.append({"logged_hours": 400, "estimated_hours": 20})
        c = calibration.compute(obs)
        assert c.factor == pytest.approx(1.0)

    def test_factor_is_clamped(self):
        obs = [{"logged_hours": 1000, "estimated_hours": 1}] * 5
        assert calibration.compute(obs).factor <= calibration.MAX_FACTOR

    def test_needs_a_minimum_sample(self):
        assert not calibration.compute(
            [{"logged_hours": 30, "estimated_hours": 20}]).confident

    def test_apply_scales_estimates(self):
        c = calibration.compute([{"logged_hours": 30, "estimated_hours": 20}] * 4)
        assert c.apply(pd.Series([10.0])).iloc[0] == pytest.approx(15.0)

    def test_velocity(self):
        sessions = [
            {"started_at": "2026-08-01T10:00:00Z", "minutes": 120},
            {"started_at": "2026-08-08T10:00:00Z", "minutes": 180},
            {"started_at": "2026-08-15T10:00:00Z", "minutes": 60},
        ]
        v = calibration.velocity(sessions)
        assert v["total_hours"] == pytest.approx(6.0)
        assert v["hours_per_week"] > 0

    def test_projection(self):
        p = calibration.project_completion(520.0, 10.0)
        assert p["weeks"] == pytest.approx(52.0)
        assert p["years"] == pytest.approx(1.0)

    def test_projection_without_a_pace(self):
        assert calibration.project_completion(100.0, 0.0)["feasible"] is False


class TestSummaryFollowsPreferences:
    """Regression: the KPI tiles ignored the goal/mode toggle.

    `summarize(df)` was called without the preference columns, so it always
    fell back to the softcore-master default. The labels changed; the numbers
    did not.
    """

    def build(self):
        return frame([
            game(1, "A", points=400, master=20.0, master_hc=26.0, beat=5.0, beat_hc=7.0),
            game(2, "B", points=300, master=10.0, master_hc=13.0, beat=2.0, beat_hc=3.0),
        ])

    def test_beat_and_master_totals_differ(self):
        from ra_backlog.efficiency import summarize
        df = self.build()
        master = summarize(df, Preferences(GOAL_MASTER, MODE_SOFTCORE).time_columns)
        beat = summarize(df, Preferences(GOAL_BEAT, MODE_SOFTCORE).time_columns)
        assert master["total_hours"] == pytest.approx(30.0)
        assert beat["total_hours"] == pytest.approx(7.0)
        assert master["total_hours"] != beat["total_hours"]

    def test_hardcore_differs_from_softcore(self):
        from ra_backlog.efficiency import summarize
        df = self.build()
        sc = summarize(df, Preferences(GOAL_BEAT, MODE_SOFTCORE).time_columns)
        hc = summarize(df, Preferences(GOAL_BEAT, MODE_HARDCORE).time_columns)
        assert sc["total_hours"] == pytest.approx(7.0)
        assert hc["total_hours"] == pytest.approx(10.0)

    def test_summary_endpoint_honours_preferences(self, tmp_path):
        """The end-to-end path that was broken."""
        from ra_backlog.storage.db import connect
        from ra_backlog.storage import repo
        from ra_backlog.models import Game
        from ra_backlog.web import routes_features
        from ra_backlog.efficiency import summarize
        from ra_backlog import preferences as prefs_module
        from ra_backlog.models import ProgressionResult

        conn = connect(tmp_path / "sum.db")
        repo.upsert_games(conn, [Game(ra_id=1, title="A", system="NES",
                                      points=400, achievements=50)])
        repo.update_game_times(conn, 1, prog=ProgressionResult(
            ra_beat=5.0, ra_master=20.0, ra_beat_hardcore=7.0, status="ok"))

        prefs_module.update(conn, goal=GOAL_MASTER, mode=MODE_SOFTCORE)
        p = prefs_module.load(conn)
        as_master = summarize(routes_features._enriched(conn), p.time_columns)

        prefs_module.update(conn, goal=GOAL_BEAT, mode=MODE_SOFTCORE)
        p = prefs_module.load(conn)
        as_beat = summarize(routes_features._enriched(conn), p.time_columns)

        conn.close()
        assert as_master["total_hours"] == pytest.approx(20.0)
        assert as_beat["total_hours"] == pytest.approx(5.0)


class TestGoalTerminology:
    """RetroAchievements calls softcore all-achievements *completion*;
    *mastery* specifically means hardcore."""

    def test_softcore_all_achievements_is_completion(self):
        assert Preferences(GOAL_MASTER, MODE_SOFTCORE).goal_label == "complete"

    def test_hardcore_all_achievements_is_mastery(self):
        assert Preferences(GOAL_MASTER, MODE_HARDCORE).goal_label == "master"

    def test_beat_is_beat_in_both_modes(self):
        assert Preferences(GOAL_BEAT, MODE_SOFTCORE).goal_label == "beat"
        assert Preferences(GOAL_BEAT, MODE_HARDCORE).goal_label == "beat"
