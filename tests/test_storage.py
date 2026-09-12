"""Tests for the SQLite layer.

The lookup-cache tests are the regression net for item #3.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ra_backlog.models import Game, LookupResult, ProgressionResult
from ra_backlog.storage import migrate, repo
from ra_backlog.storage.db import connect, transaction


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def game(ra_id=1, title="Contra", system="NES", points=400, achievements=50):
    return Game(ra_id=ra_id, title=title, system=system,
                points=points, achievements=achievements)


class TestGames:
    def test_upsert_and_load(self, conn):
        repo.upsert_games(conn, [game(1), game(2, "Gradius")])
        df = repo.load_games(conn)
        assert len(df) == 2
        assert set(df["title"]) == {"Contra", "Gradius"}

    def test_upsert_preserves_fetched_times(self, conn):
        repo.upsert_games(conn, [game(1)])
        repo.update_game_times(conn, 1, prog=ProgressionResult(
            ra_master=20.0, ra_beat=5.0, status="ok"))
        # A later list refresh must not wipe the timings.
        repo.upsert_games(conn, [game(1, points=450)])
        df = repo.load_games(conn)
        assert df.loc[0, "ra_master"] == 20.0
        assert df.loc[0, "points"] == 450

    def test_hardcore_columns_round_trip(self, conn):
        """Item #11: these were fetched but never stored anywhere visible."""
        repo.upsert_games(conn, [game(1)])
        repo.update_game_times(conn, 1, prog=ProgressionResult(
            ra_beat=5.0, ra_master=20.0,
            ra_beat_hardcore=7.5, ra_master_hardcore=26.0, status="ok"))
        df = repo.load_games(conn)
        assert df.loc[0, "ra_beat_hardcore"] == 7.5
        assert df.loc[0, "ra_master_hardcore"] == 26.0

    def test_delisted_games_are_flagged_not_deleted(self, conn):
        repo.upsert_games(conn, [game(1), game(2, "Gradius")])
        repo.update_game_times(conn, 2, prog=ProgressionResult(ra_master=9.0, status="ok"))
        repo.mark_want_to_play(conn, [1])          # game 2 dropped from the list

        assert len(repo.load_games(conn)) == 1
        everything = repo.load_games(conn, only_want_to_play=False)
        assert len(everything) == 2
        # Its timing data survived, so re-adding it costs nothing.
        row = everything[everything["ra_id"] == 2].iloc[0]
        assert row["ra_master"] == 9.0

    def test_readding_restores_the_flag(self, conn):
        repo.upsert_games(conn, [game(1), game(2, "Gradius")])
        repo.mark_want_to_play(conn, [1])
        repo.upsert_games(conn, [game(2, "Gradius")])
        assert 2 in set(repo.load_games(conn)["ra_id"])

    def test_system_filter(self, conn):
        repo.upsert_games(conn, [game(1, system="NES"), game(2, "G", system="SNES")])
        assert len(repo.load_games(conn, systems=["SNES"])) == 1


class TestLookupCache:
    """Item #3: successes are permanent, failures expire."""

    def test_success_is_cached(self, conn):
        result = LookupResult(status="ok", beat=5.0, complete=12.0,
                              hltb_name="Contra", similarity=1.0, quality="exact")
        repo.put_lookup(conn, "Contra|NES", result)
        cached = repo.get_lookup(conn, "Contra|NES")
        assert cached is not None
        assert cached.hltb_name == "Contra"
        assert cached.complete == 12.0

    def test_no_match_is_cached_permanently(self, conn):
        repo.put_lookup(conn, "Obscure|NES", LookupResult(status="no_match", quality="none"))
        cached = repo.get_lookup(conn, "Obscure|NES")
        assert cached is not None and cached.status == "no_match"

    def test_fresh_error_is_returned_as_an_error(self, conn):
        repo.put_lookup(conn, "Contra|NES", LookupResult(status="error", error="timeout"))
        cached = repo.get_lookup(conn, "Contra|NES")
        assert cached is not None and cached.status == "error"

    def test_stale_error_reads_as_a_cache_miss(self, conn):
        """The core of item #3: a transient failure must not be permanent."""
        repo.put_lookup(conn, "Contra|NES", LookupResult(status="error", error="timeout"))
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE lookups SET fetched_at=? WHERE cache_key=?",
                     (stale, "Contra|NES"))
        assert repo.get_lookup(conn, "Contra|NES") is None

    def test_stale_success_is_still_returned(self, conn):
        repo.put_lookup(conn, "Contra|NES", LookupResult(status="ok", hltb_name="Contra"))
        stale = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE lookups SET fetched_at=? WHERE cache_key=?",
                     (stale, "Contra|NES"))
        assert repo.get_lookup(conn, "Contra|NES") is not None

    def test_clear_failed_only_removes_errors(self, conn):
        repo.put_lookup(conn, "a", LookupResult(status="ok", hltb_name="A"))
        repo.put_lookup(conn, "b", LookupResult(status="error", error="boom"))
        assert repo.clear_failed_lookups(conn) == 1
        assert repo.get_lookup(conn, "a") is not None

    def test_missing_key(self, conn):
        assert repo.get_lookup(conn, "nope") is None


class TestTransactions:
    def test_rollback_on_exception(self, conn):
        """Item #5: a crash mid-write leaves the store as it was."""
        repo.upsert_games(conn, [game(1)])
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("UPDATE games SET title='corrupted' WHERE ra_id=1")
                raise RuntimeError("interrupted")
        assert repo.load_games(conn).loc[0, "title"] == "Contra"


class TestMigration:
    def test_imports_legacy_excel(self, conn, tmp_path, monkeypatch):
        import pandas as pd
        legacy = tmp_path / "HowLongToBeat.xlsx"
        pd.DataFrame([{
            "Title": "Contra", "System": "NES", "Achievements": 50, "Points": 400,
            "RA_ID": 1, "HLTB_Beat": 2.0, "HLTB_Complete": 5.0,
            "RA_Beat": 3.0, "RA_Master": 20.0, "RA_Players": 900,
        }]).to_excel(legacy, index=False)

        migrate._import_excel(conn, legacy)
        df = repo.load_games(conn)
        assert df.loc[0, "title"] == "Contra"
        assert df.loc[0, "ra_master"] == 20.0

    def test_legacy_errors_are_not_imported_as_permanent(self, conn, tmp_path):
        """A failure in the old cache should be retried, not inherited."""
        legacy = tmp_path / "hltb_progress.json"
        legacy.write_text(json.dumps({
            "Good|NES": {"beat": 2.0, "complete": 5.0, "hltb_name": "Good",
                         "similarity": 1.0, "comment": None},
            "Broken|NES": {"error": "Connection reset", "comment": "Error: ..."},
        }), encoding="utf-8")

        imported = migrate._import_progress(conn, legacy)
        assert imported == 1
        assert repo.get_lookup(conn, "Good|NES") is not None
        assert repo.get_lookup(conn, "Broken|NES") is None

    def test_corrupt_progress_file_does_not_raise(self, conn, tmp_path):
        bad = tmp_path / "hltb_progress.json"
        bad.write_text('{"truncated": ', encoding="utf-8")
        assert migrate._import_progress(conn, bad) == 0
