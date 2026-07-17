import zipfile

import pytest

from ermlib import cli, paths
from ermlib import state as state_mod
from ermlib.errors import PathError


def _make_ersc_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def _seed_profile(tmp_path, name="seamless-only"):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test profile"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
        'install = "game"\n'
    )


def _seed_apply_fixture(tmp_path, game_dir):
    _seed_profile(tmp_path)
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'asset = "seamless-coop-v1.9.8.zip"\n'
        'sha256 = "a"\n'
        'source = "github"\n'
    )
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _make_ersc_zip(vendor / "seamless-coop-v1.9.8.zip")


def test_launch_option_string(capsys):
    # reshade=False and me3=False are explicit so the assertion is deterministic
    # regardless of whether the machine running the tests happens to have ReShade
    # installed, or the cwd's installed.json happens to have me3 packages recorded.
    rc = cli.cmd_launch_option(type("A", (), {"json": False, "reshade": False, "me3": False})())
    out = capsys.readouterr().out
    assert 'start_protected_game.exe/ersc_launcher.exe' in out
    assert out.count('"') >= 2          # quoting preserved
    assert "WINEDLLOVERRIDES" not in out
    assert rc == 0


def test_launch_option_prepends_reshade_override_when_present(capsys):
    rc = cli.cmd_launch_option(type("A", (), {"json": False, "reshade": True, "me3": False})())
    out = capsys.readouterr().out
    # the ReShade dxgi override is prepended, and the ERSC wrapper is preserved
    assert 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b"' in out
    assert 'start_protected_game.exe/ersc_launcher.exe' in out
    # and it warns that this variant is per-machine (don't hand it to the Deck)
    assert "per-machine" in out.lower()
    assert rc == 0


def test_build_launch_option_helper():
    assert cli.build_launch_option(False, False) == cli.LAUNCH_OPTION
    reshaded = cli.build_launch_option(True, False)
    assert reshaded.startswith('WINEDLLOVERRIDES=')
    assert reshaded.endswith(cli.LAUNCH_OPTION)


def test_build_launch_option_me3_mode_uses_me3_command():
    assert cli.build_launch_option(False, True) == cli.ME3_LAUNCH
    assert cli.build_launch_option(False, False) == cli.LAUNCH_OPTION
    withrs = cli.build_launch_option(True, True)
    assert withrs.startswith("WINEDLLOVERRIDES=")
    assert cli.ME3_LAUNCH in withrs


def test_launch_option_me3_flag_forces_me3(capsys):
    cli.cmd_launch_option(type("A", (), {"json": False, "reshade": False, "me3": True})())
    out = capsys.readouterr().out
    assert cli.ME3_LAUNCH in out


def test_launch_option_ersc_flag_forces_wrapper(capsys):
    cli.cmd_launch_option(type("A", (), {"json": False, "reshade": False, "me3": False})())
    out = capsys.readouterr().out
    assert "ersc_launcher.exe" in out
    assert cli.ME3_LAUNCH not in out


def test_me3_launch_constant_is_nonempty_and_names_the_profile():
    # Verified live: me3 launches ELDEN RING + Seamless on Proton with this command.
    assert cli.ME3_LAUNCH.strip()
    assert "erm-coop.me3" in cli.ME3_LAUNCH


def test_audit_on_fixture_save(capsys, tmp_path):
    from tests.conftest import REAL_SAVE
    if not REAL_SAVE.exists():
        pytest.skip("no fixture")
    args = type("A", (), {"json": False, "save": str(REAL_SAVE)})()
    rc = cli.cmd_audit(args)
    out = capsys.readouterr().out
    assert "cannot" in out.lower()      # the honesty caveat always prints
    assert rc == 0


def test_audit_bad_path_raises_patherror():
    args = type("A", (), {"json": False, "save": "/nonexistent/ER0000.sl2"})()
    with pytest.raises(PathError):
        cli.cmd_audit(args)


def test_restore_resolves_snapshot_name_under_backups(tmp_path, monkeypatch):
    # `erm restore <name>` takes a snapshot NAME from backups/, per the README —
    # not a cwd-relative path. Seed backups/snap.co2 and confirm restore pulls
    # from there rather than failing to find a "snap.co2" next to the cwd.
    from ermlib import paths

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "snap.co2").write_bytes(b"snapshot-data")

    save_dir = tmp_path / "save"
    save_dir.mkdir()

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)

    args = type("A", (), {"backup": "snap.co2"})()
    rc = cli.cmd_restore(args)
    assert rc == 0
    assert (save_dir / "ER0000.co2").read_bytes() == b"snapshot-data"


def test_apply_missing_vendor_archive_warns_and_continues(tmp_path, monkeypatch, capsys):
    # Fresh clone that runs `apply` before `fetch`: the lockfile names an asset
    # that was never downloaded. This is now a per-mod warning (like "not
    # fetched"), not a raised error — apply keeps going, records nothing for
    # that mod, and still runs doctor rather than crashing the whole command.
    from ermlib import paths

    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _seed_profile(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'asset = "seamless-coop-v1.9.8.zip"\n'
        'sha256 = "abc"\n'
        'source = "github"\n'
    )

    args = type("A", (), {"profile": "seamless-only", "json": False})()
    cli.cmd_apply(args)
    out = capsys.readouterr().out
    assert "archive missing from vendor" in out.lower()

    import json
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "seamless-coop" not in state


def test_apply_prints_doctor_section_and_returns_ok_when_clean(tmp_path, monkeypatch, capsys):
    # `erm apply` must run the doctor safety check right after installing, so
    # a dangerous post-apply state is loud at the moment of apply rather than
    # silent until someone remembers to run `erm doctor` separately.
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "start_protected_game.exe").write_bytes(b"\x00")
    _seed_apply_fixture(tmp_path, game_dir)

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    args = type("A", (), {"profile": "seamless-only", "json": False})()
    rc = cli.cmd_apply(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "doctor" in out.lower()
    assert "no proxy dll" in out.lower()


def test_apply_returns_doctor_fail_code_when_forbidden_artifact_present(tmp_path, monkeypatch, capsys):
    # A leftover proxy DLL alongside start_protected_game.exe is the exact
    # dangerous mixed state doctor fails on — apply must surface that failure
    # in its own exit code, not just print it and return 0 regardless.
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "start_protected_game.exe").write_bytes(b"\x00")
    (game_dir / "dinput8.dll").write_bytes(b"\x00")
    _seed_apply_fixture(tmp_path, game_dir)

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    args = type("A", (), {"profile": "seamless-only", "json": False})()
    rc = cli.cmd_apply(args)
    out = capsys.readouterr().out

    assert rc == 1
    assert "✗" in out or "fail" in out.lower()


def test_status_lists_installed_mods(tmp_path, monkeypatch, capsys):
    # `erm status` must surface what's actually recorded in installed.json —
    # a game-installed mod and a me3 package look different (different kind
    # tag), and having any me3 package present flips the launch mode note.
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    state = state_mod.load_state()
    state_mod.record_install(state, "seamless-coop", "v1.9.8",
                              "seamless-coop-v1.9.8.zip", ["ersc_launcher.exe"])
    state_mod.record_me3_package(state, "minimal-hud", "1.0", "MinimalHUD.zip",
                                  "tools/me3/mods/minimal-hud")
    state_mod.write_state(tmp_path / "installed.json", state)

    args = type("A", (), {"json": False})()
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "seamless-coop" in out
    assert "v1.9.8" in out
    assert "(game)" in out
    assert "minimal-hud" in out
    assert "1.0" in out
    assert "(me3-package)" in out
    assert "2 mod(s) installed" in out
    assert "me3-mode" in out.lower()
    assert "launch-option" in out


def test_status_no_installed_json_says_none_recorded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    args = type("A", (), {"json": False})()
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "no mods recorded in installed.json" in out.lower()


def test_status_corrupt_installed_json_warns_instead_of_crashing(tmp_path, monkeypatch, capsys):
    # load_state raises ErmError on a corrupted installed.json; status must
    # warn and keep going (still prints the game/cloud-save lines) rather
    # than letting the exception blow up the whole command.
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "installed.json").write_text("{not valid json")

    args = type("A", (), {"json": False})()
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "corrupt" in out.lower()
    assert "game installed" in out.lower()


def test_verify_missing_asset_key_warns_instead_of_crashing(tmp_path, monkeypatch, capsys):
    # A lock entry missing "asset" made Path("vendor")/"" resolve to the vendor
    # dir itself -> IsADirectoryError from sha256_file. Must warn and move on.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'source = "github"\n'
    )
    args = type("A", (), {"json": False})()
    rc = cli.cmd_verify(args)
    out = capsys.readouterr().out
    assert "no asset" in out.lower()
