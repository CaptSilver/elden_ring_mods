import json
import shutil
from pathlib import Path

from ermlib import cli, paths, tidy


# ---------- find_cruft: safety-case unit tests ----------

def test_orphaned_mod_dir_is_removed_when_dll_absent(tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "AdjustTheFov"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    cruft = tidy.find_cruft(game, recorded=set())

    assert mod_dir in cruft


def test_mod_dir_left_alone_when_dll_still_installed(tmp_game):
    game = tmp_game
    mods = game / "mods"
    mods.mkdir()
    mod_dir = mods / "CameraFix"
    mod_dir.mkdir()
    (mod_dir / "log.txt").write_text("log")
    (mods / "CameraFix.dll").write_bytes(b"\x00")   # mod still installed

    cruft = tidy.find_cruft(game, recorded=set())

    assert mod_dir not in cruft


def test_mod_dir_with_non_runtime_file_is_left_alone(tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "Weird"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")
    (mod_dir / "payload.bin").write_bytes(b"\x00")   # not a recognized runtime type

    cruft = tidy.find_cruft(game, recorded=set())

    assert mod_dir not in cruft


def test_recorded_file_is_never_a_candidate(tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "Active"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    # matches the orphaned-dir pattern, but log.txt is recorded as an active
    # mod file — the whole dir must be excluded.
    cruft = tidy.find_cruft(game, recorded={"mods/Active/log.txt"})

    assert mod_dir not in cruft
    assert (mod_dir / "log.txt") not in cruft


def test_symlink_escape_is_skipped_and_nothing_returned_resolves_outside_game(tmp_game, tmp_path):
    game = tmp_game
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("precious")

    escape = game / "mods" / "escape"
    escape.parent.mkdir(parents=True, exist_ok=True)
    escape.symlink_to(outside)

    cruft = tidy.find_cruft(game, recorded=set())

    assert escape not in cruft
    game_res = game.resolve()
    for c in cruft:
        try:
            c.resolve().relative_to(game_res)
        except ValueError:
            raise AssertionError(f"{c} resolves outside Game/")


def test_critical_file_is_never_a_candidate(tmp_game):
    game = tmp_game
    # A mod dir literally named after a CRITICAL filename — it otherwise
    # qualifies as an orphaned runtime dir (no matching .dll, only a runtime
    # file inside), but the CRITICAL gate in consider() must still block it.
    mod_dir = game / "mods" / "eldenring.exe"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    cruft = tidy.find_cruft(game, recorded=set())

    assert mod_dir not in cruft
    # the real stock file tmp_game seeds at the game root must never appear either
    assert (game / "eldenring.exe") not in cruft


def test_crashdumps_removed_only_when_ersc_dll_absent(tmp_game):
    game = tmp_game
    sc = game / "SeamlessCoop"
    sc.mkdir()
    crashdumps = sc / "crashdumps"
    crashdumps.mkdir()
    (crashdumps / "dump1.dmp").write_bytes(b"\x00")

    cruft = tidy.find_cruft(game, recorded=set())
    assert crashdumps in cruft

    (sc / "ersc.dll").write_bytes(b"\x00")   # ERSC still installed
    cruft = tidy.find_cruft(game, recorded=set())
    assert crashdumps not in cruft


def test_loose_logs_removed(tmp_game):
    game = tmp_game
    (game / "mod_loader_log.txt").write_text("log")
    mods = game / "mods"
    mods.mkdir()
    (mods / "RandomizerHelper_log.txt").write_text("log")

    cruft = tidy.find_cruft(game, recorded=set())

    assert (game / "mod_loader_log.txt") in cruft
    assert (mods / "RandomizerHelper_log.txt") in cruft


# ---------- cmd_tidy: CLI-level behavior ----------

def _tidy_args(apply=False, json_out=False):
    return type("A", (), {"apply": apply, "json": json_out})()


def test_cmd_tidy_dry_run_default_deletes_nothing(tmp_path, monkeypatch, capsys, tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "AdjustTheFov"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_tidy(_tidy_args(apply=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert mod_dir.exists()
    assert (mod_dir / "log.txt").exists()
    assert "would remove" in out.lower()


def test_cmd_tidy_apply_deletes_candidates(tmp_path, monkeypatch, capsys, tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "AdjustTheFov"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_tidy(_tidy_args(apply=True))
    capsys.readouterr()

    assert rc == 0
    assert not mod_dir.exists()


def test_cmd_tidy_never_removes_recorded_file(tmp_path, monkeypatch, capsys, tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "Active"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")

    (tmp_path / "installed.json").write_text(json.dumps({
        "Active": {"version": "v1", "archive": "x.zip", "files": ["mods/Active/log.txt"]},
    }))

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_tidy(_tidy_args(apply=True))
    capsys.readouterr()

    assert rc == 0
    assert mod_dir.exists()
    assert (mod_dir / "log.txt").exists()


def test_cmd_tidy_apply_oserror_warns_and_continues(tmp_path, monkeypatch, capsys, tmp_game):
    game = tmp_game
    mod_dir = game / "mods" / "AdjustTheFov"
    mod_dir.mkdir(parents=True)
    (mod_dir / "log.txt").write_text("log")
    (game / "mod_loader_log.txt").write_text("log")   # second, unrelated candidate

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)
    monkeypatch.chdir(tmp_path)

    real_rmtree = shutil.rmtree

    def boom(path, *a, **kw):
        if Path(path) == mod_dir:
            raise OSError("permission denied")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(cli.shutil, "rmtree", boom)

    rc = cli.cmd_tidy(_tidy_args(apply=True))
    out = capsys.readouterr().out

    assert rc == 0                                      # doesn't crash
    assert mod_dir.exists()                              # failed removal left in place
    assert not (game / "mod_loader_log.txt").exists()    # other candidate still removed
    assert "could not remove" in out.lower()
