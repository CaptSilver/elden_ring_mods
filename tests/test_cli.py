import importlib.machinery
import importlib.util
import json
import pathlib
import zipfile

import pytest

from ermlib import cli, paths
from ermlib import state as state_mod
from ermlib.errors import PathError

_ERM = pathlib.Path(__file__).resolve().parent.parent / "erm"
_spec = importlib.util.spec_from_loader(
    "erm_cli", importlib.machinery.SourceFileLoader("erm_cli", str(_ERM)))
_erm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_erm)
build_parser = _erm.build_parser


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


@pytest.fixture
def pinned_machine(monkeypatch, tmp_path):
    """Pin everything cmd_launch_option reads off this machine.

    It looks up three things: the me3 binary, whether ReShade is linked into the
    game dir, and whether installed.json records me3 packages. Unpinned, these
    assertions would pass or fail depending on the box running them — the flags
    that used to make this deterministic are gone.
    """
    monkeypatch.setattr(cli.launch, "find_me3", lambda: pathlib.Path("/opt/me3"))
    monkeypatch.setattr(cli.launch, "PROFILE", tmp_path / "erm-coop.me3")
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: tmp_path)
    monkeypatch.setattr(paths, "reshade_active", lambda g: False)
    monkeypatch.chdir(tmp_path)          # no installed.json -> no me3 packages
    return tmp_path


def _launch_out(capsys, json_mode=False):
    rc = cli.cmd_launch_option(type("A", (), {"json": json_mode})())
    assert rc == 0
    return capsys.readouterr().out


def test_launch_option_prints_every_variant_in_one_run(pinned_machine, capsys):
    out = _launch_out(capsys)
    # LAUNCH_OPTION is a substring of RESHADE_ENV + LAUNCH_OPTION, so a bare
    # membership check would pass even if only the ReShade line printed and
    # the plain one got dropped. Pin the plain line's own framing instead.
    assert f"  plain\n    {cli.LAUNCH_OPTION}\n" in out
    assert cli.RESHADE_ENV + cli.LAUNCH_OPTION in out
    assert cli.LAUNCH_VALIDATOR in out
    assert "Dual GPU" in out


def test_launch_option_keeps_steam_launch_options_framing(pinned_machine, capsys):
    out = _launch_out(capsys)
    assert "Steam → ELDEN RING → Properties → Launch Options" in out


def test_launch_option_me3_command_is_a_steam_launch_option(pinned_machine, capsys):
    # The me3 command belongs in Steam's Launch Options like the ersc wrapper.
    # `# %command%` comments out the Proton chain Steam substitutes; without the
    # token Steam appends the field as argv to the game exe and vanilla boots
    # with no mods and no error. An earlier version of this command blamed that
    # on me3 and told you to run it from a terminal instead.
    out = _launch_out(capsys)
    # The explanatory paragraph below also contains the literal text
    # "# %command%" (it's discussing the token), so a bare membership check
    # would pass even if the rendered command itself dropped it. Assert
    # against the actual plain me3 command line instead.
    plain_me3 = cli.launch.me3_command(pathlib.Path("/opt/me3"), pinned_machine / "erm-coop.me3")
    assert plain_me3.endswith("# %command%")
    assert f"  plain\n    {plain_me3}\n" in out
    assert "terminal" not in out.lower()


def test_launch_option_reports_a_missing_me3_binary(monkeypatch, pinned_machine, capsys):
    monkeypatch.setattr(cli.launch, "find_me3", lambda: None)
    out = _launch_out(capsys)
    assert "me3 is not installed on this machine" in out
    # ersc still printed — it does not need me3. Pin the plain line's own
    # framing, not a bare substring (it's also a substring of the ReShade form).
    assert f"  plain\n    {cli.LAUNCH_OPTION}\n" in out


def test_launch_option_shows_clean_output_when_profile_exists(pinned_machine, capsys):
    # pinned_machine never creates erm-coop.me3, so every other test here
    # exercises the "does not exist yet" warning path. Create it and confirm
    # the warning drops out while the commands themselves are unaffected.
    (pinned_machine / "erm-coop.me3").write_text("")
    out = _launch_out(capsys)
    assert "does not exist yet" not in out
    plain_me3 = cli.launch.me3_command(pathlib.Path("/opt/me3"), pinned_machine / "erm-coop.me3")
    assert f"  plain\n    {plain_me3}\n" in out
    assert f"  plain\n    {cli.LAUNCH_OPTION}\n" in out


def test_launch_option_takes_no_filter_flags():
    # --me3/--ersc/--reshade/--no-reshade existed only to override auto-detection.
    # Nothing is auto-detected now, so they are gone.
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["launch-option", "--me3"])
    with pytest.raises(SystemExit):
        parser.parse_args(["launch-option", "--reshade"])


def test_launch_option_no_longer_exposes_a_single_string_builder():
    assert not hasattr(cli, "build_launch_option")
    assert not hasattr(cli, "ME3_LAUNCH")


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
    # that was never downloaded, so apply now auto-fetches it. Here the fetch
    # fails (simulated offline) — apply must warn, install what's present, record
    # nothing for that mod, and still run doctor rather than crashing.
    import urllib.error
    from ermlib import paths, github

    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    # Auto-fetch would reach for GitHub; simulate no network so it fails cleanly
    # instead of downloading the real release.
    def offline(*a, **k):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(github, "release_by_tag", offline)
    monkeypatch.setattr(github, "latest_release", offline)

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
    assert "auto-fetch incomplete" in out.lower()      # fetch was attempted, failed gracefully
    assert "archive missing from vendor" in out.lower()  # still-missing mod warned, not crashed

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


def test_launch_option_json_emits_the_variant_set(pinned_machine, capsys):
    # --json was accepted and ignored before, printing prose regardless.
    data = json.loads(_launch_out(capsys, json_mode=True))
    assert data["ersc"]["plain"] == cli.LAUNCH_OPTION
    assert data["ersc"]["reshade"] == cli.RESHADE_ENV + cli.LAUNCH_OPTION
    assert data["validator"] == cli.LAUNCH_VALIDATOR
    assert data["me3"]["plain"].endswith("# %command%")
    for key in ("reshade_installed", "me3_packages", "profile_exists"):
        assert isinstance(data[key], bool)


def test_launch_option_json_me3_is_null_when_binary_missing(
        monkeypatch, pinned_machine, capsys):
    monkeypatch.setattr(cli.launch, "find_me3", lambda: None)
    data = json.loads(_launch_out(capsys, json_mode=True))
    assert data["me3"] is None


def test_launch_option_json_emits_no_prose(pinned_machine, capsys):
    out = _launch_out(capsys, json_mode=True)
    assert "Steam → ELDEN RING" not in out
    assert "Dual GPU" not in out
