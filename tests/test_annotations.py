"""Tests for user annotation storage and the ROM launcher's guard rails."""
from __future__ import annotations

import pytest

from ra_backlog import launcher
from ra_backlog.launcher import LaunchError, LaunchRoots
from ra_backlog.models import Game
from ra_backlog.storage import annotations, repo
from ra_backlog.storage.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "ann.db")
    repo.upsert_games(c, [
        Game(ra_id=1, title="Contra", system="NES", points=400, achievements=50),
        Game(ra_id=2, title="Gradius", system="NES", points=300, achievements=40),
    ])
    yield c
    c.close()


class TestTags:
    def test_tag_and_list(self, conn):
        annotations.tag_game(conn, 1, "short")
        annotations.tag_game(conn, 1, "nostalgia")
        annotations.tag_game(conn, 2, "short")
        assert annotations.tags_for_games(conn)[1] == ["nostalgia", "short"]
        assert {t["name"]: t["uses"] for t in annotations.list_tags(conn)}["short"] == 2

    def test_tagging_twice_is_idempotent(self, conn):
        annotations.tag_game(conn, 1, "short")
        annotations.tag_game(conn, 1, "short")
        assert annotations.tags_for_games(conn)[1] == ["short"]

    def test_tag_names_are_case_insensitive(self, conn):
        annotations.tag_game(conn, 1, "Short")
        annotations.tag_game(conn, 2, "short")
        assert len(annotations.list_tags(conn)) == 1

    def test_untag(self, conn):
        tag_id = annotations.tag_game(conn, 1, "short")
        annotations.untag_game(conn, 1, tag_id)
        assert 1 not in annotations.tags_for_games(conn)

    def test_empty_name_rejected(self, conn):
        with pytest.raises(ValueError):
            annotations.create_tag(conn, "   ")

    def test_deleting_a_tag_removes_its_links(self, conn):
        tag_id = annotations.tag_game(conn, 1, "short")
        annotations.delete_tag(conn, tag_id)
        assert annotations.tags_for_games(conn) == {}


class TestAnnotations:
    def test_note_pin_hide(self, conn):
        annotations.set_annotation(conn, 1, note="tough final boss", pinned=True)
        df = annotations.load_annotations(conn)
        row = df[df["ra_id"] == 1].iloc[0]
        assert row["note"] == "tough final boss"
        assert row["pinned"] == 1

    def test_partial_update_preserves_other_fields(self, conn):
        annotations.set_annotation(conn, 1, note="keep me", pinned=True)
        annotations.set_annotation(conn, 1, hidden=True)
        row = annotations.load_annotations(conn).iloc[0]
        assert row["note"] == "keep me" and row["pinned"] == 1 and row["hidden"] == 1

    def test_survives_a_rescan(self, conn):
        """Nothing a scan does may touch what the user typed."""
        annotations.set_annotation(conn, 1, note="mine", pinned=True)
        repo.upsert_games(conn, [Game(ra_id=1, title="Contra", system="NES",
                                      points=999, achievements=60)])
        row = annotations.load_annotations(conn).iloc[0]
        assert row["note"] == "mine" and row["pinned"] == 1

    def test_unknown_fields_ignored(self, conn):
        annotations.set_annotation(conn, 1, note="ok", nonsense="dropped")
        assert annotations.load_annotations(conn).iloc[0]["note"] == "ok"


class TestViews:
    def test_save_and_list(self, conn):
        annotations.save_view(conn, "Quick wins",
                              {"max_hours": 5, "min_pph": 50})
        views = annotations.list_views(conn)
        assert views[0]["name"] == "Quick wins"
        assert views[0]["payload"]["max_hours"] == 5

    def test_resaving_replaces(self, conn):
        annotations.save_view(conn, "V", {"a": 1})
        annotations.save_view(conn, "V", {"a": 2})
        views = annotations.list_views(conn)
        assert len(views) == 1 and views[0]["payload"]["a"] == 2

    def test_delete(self, conn):
        vid = annotations.save_view(conn, "V", {})
        annotations.delete_view(conn, vid)
        assert annotations.list_views(conn) == []


class TestAchievements:
    def test_replace_and_read(self, conn):
        rows = [
            {"achievement_id": 10, "title": "Start", "points": 5,
             "num_awarded": 900, "total_players": 1000, "display_order": 1},
            {"achievement_id": 11, "title": "Wall", "points": 50,
             "num_awarded": 4, "total_players": 1000, "display_order": 2},
        ]
        assert annotations.replace_achievements(conn, 1, rows) == 2
        got = annotations.achievements_for(conn, 1)
        assert [a["title"] for a in got] == ["Start", "Wall"]

    def test_replace_is_a_swap_not_an_append(self, conn):
        annotations.replace_achievements(conn, 1, [{"achievement_id": 10, "title": "A"}])
        annotations.replace_achievements(conn, 1, [{"achievement_id": 11, "title": "B"}])
        assert [a["title"] for a in annotations.achievements_for(conn, 1)] == ["B"]

    def test_rarity_is_stored_on_the_game(self, conn):
        annotations.store_rarity(conn, 1, 72.5, 0.4)
        row = repo.load_games(conn)
        row = row[row["ra_id"] == 1].iloc[0]
        assert row["rarity_score"] == 72.5

    def test_needing_achievements_skips_recent(self, conn):
        assert 1 in annotations.games_needing_achievements(conn)
        annotations.replace_achievements(conn, 1, [{"achievement_id": 1, "title": "A"}])
        assert 1 not in annotations.games_needing_achievements(conn)


class TestSessions:
    def test_start_and_finish(self, conn):
        sid = annotations.start_session(conn, 1)
        assert annotations.open_session(conn)["session_id"] == sid
        annotations.finish_session(conn, sid, minutes=90)
        assert annotations.open_session(conn) is None
        assert annotations.logged_hours_by_game(conn)[1] == pytest.approx(1.5)

    def test_manual_log(self, conn):
        annotations.log_session(conn, 1, minutes=120, note="evening")
        assert annotations.logged_hours_by_game(conn)[1] == pytest.approx(2.0)

    def test_hours_accumulate(self, conn):
        annotations.log_session(conn, 1, minutes=60)
        annotations.log_session(conn, 1, minutes=30)
        assert annotations.logged_hours_by_game(conn)[1] == pytest.approx(1.5)

    def test_finishing_an_unknown_session_is_a_noop(self, conn):
        annotations.finish_session(conn, 9999, minutes=10)   # must not raise


class TestEvents:
    def test_replace_and_read_active(self, conn):
        annotations.replace_events(conn, [
            {"name": "AotW", "ra_id": 1, "starts_at": "2026-08-01",
             "ends_at": "2099-01-01", "url": "https://example.invalid"},
            {"name": "Old", "ra_id": 2, "starts_at": "2020-01-01",
             "ends_at": "2020-02-01"},
        ])
        active = annotations.active_events(conn)
        assert [e["name"] for e in active] == ["AotW"]
        assert active[0]["title"] == "Contra"


class TestLauncherSecurity:
    """The launcher must never act on a path supplied by a request."""

    def test_rejects_paths_outside_the_allowed_roots(self, tmp_path):
        allowed = tmp_path / "roms"
        allowed.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        rom = outside / "evil.nes"
        rom.write_bytes(b"x")

        roots = LaunchRoots.from_strings([str(allowed)])
        with pytest.raises(LaunchError, match="outside"):
            launcher.validate_rom(str(rom), roots)

    def test_rejects_traversal_out_of_a_root(self, tmp_path):
        allowed = tmp_path / "roms"
        allowed.mkdir()
        secret = tmp_path / "secret.nes"
        secret.write_bytes(b"x")

        roots = LaunchRoots.from_strings([str(allowed)])
        with pytest.raises(LaunchError):
            launcher.validate_rom(str(allowed / ".." / "secret.nes"), roots)

    def test_rejects_unknown_file_types(self, tmp_path):
        allowed = tmp_path / "roms"
        allowed.mkdir()
        script = allowed / "payload.bat"
        script.write_text("echo hi")
        roots = LaunchRoots.from_strings([str(allowed)])
        with pytest.raises(LaunchError, match="Unexpected file type"):
            launcher.validate_rom(str(script), roots)

    def test_rejects_missing_files(self, tmp_path):
        allowed = tmp_path / "roms"
        allowed.mkdir()
        roots = LaunchRoots.from_strings([str(allowed)])
        with pytest.raises(LaunchError, match="No such file"):
            launcher.validate_rom(str(allowed / "ghost.nes"), roots)

    def test_no_roots_means_nothing_is_allowed(self, tmp_path):
        rom = tmp_path / "game.nes"
        rom.write_bytes(b"x")
        with pytest.raises(LaunchError):
            launcher.validate_rom(str(rom), LaunchRoots())

    def test_accepts_a_legitimate_rom(self, tmp_path):
        allowed = tmp_path / "roms"
        allowed.mkdir()
        rom = allowed / "contra.nes"
        rom.write_bytes(b"x")
        roots = LaunchRoots.from_strings([str(allowed)])
        assert launcher.validate_rom(str(rom), roots) == rom.resolve()

    def test_unknown_emulator_rejected(self):
        with pytest.raises(LaunchError, match="Unknown emulator"):
            launcher.resolve_emulator("rm -rf /")

    def test_extra_args_are_argv_not_shell(self, tmp_path, monkeypatch):
        """Arguments must be split as argv, never handed to a shell."""
        monkeypatch.setattr(launcher, "_find_executable", lambda name: "/usr/bin/mgba")
        rom = tmp_path / "g.gba"
        rom.write_bytes(b"x")
        cmd = launcher.build_command(rom, "mgba", None, "--fullscreen && rm -rf /")
        assert "&&" in cmd            # present as a literal argument...
        assert not any(";" in part and " " in part for part in cmd)
        assert cmd[0] == "/usr/bin/mgba"

    def test_launch_refuses_an_unregistered_game(self):
        with pytest.raises(LaunchError, match="No ROM registered"):
            launcher.launch({}, LaunchRoots())

    def test_launch_rechecks_roots_at_launch_time(self, tmp_path, monkeypatch):
        """Narrowing the roots after registration must take effect immediately."""
        allowed = tmp_path / "roms"
        allowed.mkdir()
        rom = allowed / "contra.nes"
        rom.write_bytes(b"x")
        monkeypatch.setattr(launcher, "_find_executable", lambda name: "/usr/bin/mgba")

        target = {"rom_path": str(rom), "emulator": "mgba", "core": None,
                  "extra_args": None}
        with pytest.raises(LaunchError, match="no longer inside"):
            launcher.launch(target, LaunchRoots())      # roots narrowed to nothing


class TestLaunchTargets:
    def test_register_and_read(self, conn):
        annotations.set_launch_target(conn, 1, "/roms/contra.nes", "mgba")
        target = annotations.get_launch_target(conn, 1)
        assert target["rom_path"] == "/roms/contra.nes"
        assert target["emulator"] == "mgba"

    def test_reregistering_replaces(self, conn):
        annotations.set_launch_target(conn, 1, "/roms/a.nes", "mgba")
        annotations.set_launch_target(conn, 1, "/roms/b.nes", "mesen")
        assert annotations.get_launch_target(conn, 1)["rom_path"] == "/roms/b.nes"

    def test_delete(self, conn):
        annotations.set_launch_target(conn, 1, "/roms/a.nes", "mgba")
        annotations.delete_launch_target(conn, 1)
        assert annotations.get_launch_target(conn, 1) is None
