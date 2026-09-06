import importlib.machinery
import importlib.util
import json
import pathlib

import pytest

from ermlib import cli, paths
from ermlib import state as state_mod
from ermlib.errors import PathError
from tests.build_fixtures import build_id
from tests.ersc_fixtures import make_ersc_zip, seed_profile

_ERM = pathlib.Path(__file__).resolve().parent.parent / "erm"
_spec = importlib.util.spec_from_loader(
    "erm_cli", importlib.machinery.SourceFileLoader("erm_cli", str(_ERM)))
_erm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_erm)
build_parser = _erm.build_parser
main = _erm.main


def _seed_apply_fixture(tmp_path, game_dir):
    seed_profile(tmp_path)
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'asset = "seamless-coop-v1.9.8.zip"\n'
        'sha256 = "a"\n'
        'source = "github"\n'
    )
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    make_ersc_zip(vendor / "seamless-coop-v1.9.8.zip")


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
    # The me3 command belongs in Steam's Launch Options like the ersc wrapper, not
    # a terminal. `# %command%` comments out the Proton chain Steam substitutes;
    # without the token Steam appends the field as argv to the game exe and
    # vanilla boots with no mods and no error.
    out = _launch_out(capsys)
    # The explanatory paragraph below also contains the literal text
    # "# %command%" (it's discussing the token), so a bare membership check
    # would pass even if the rendered command itself dropped it. Assert
    # against the actual plain me3 command line instead.
    plain_me3 = cli.launch.me3_command(pathlib.Path("/opt/me3"), pinned_machine / "erm-coop.me3")
    assert plain_me3.endswith(" # %command%")
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

    seed_profile(tmp_path)
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
    assert data["me3"]["plain"].endswith(" # %command%")
    for key in ("reshade_installed", "me3_packages", "profile_exists"):
        assert isinstance(data[key], bool)
    # The profile actually used to build the me3 command above — a consumer
    # shouldn't have to parse it back out of me3.plain.
    assert data["profile"] == str(pinned_machine / "erm-coop.me3")
    assert pathlib.Path(data["profile"]).is_absolute()


def test_launch_option_json_me3_is_null_when_binary_missing(
        monkeypatch, pinned_machine, capsys):
    monkeypatch.setattr(cli.launch, "find_me3", lambda: None)
    data = json.loads(_launch_out(capsys, json_mode=True))
    assert data["me3"] is None


def test_launch_option_json_emits_no_prose(pinned_machine, capsys):
    out = _launch_out(capsys, json_mode=True)
    assert "Steam → ELDEN RING" not in out
    assert "Dual GPU" not in out


def _refresh_args(dry_run=False, no_reharden=False, json_out=False):
    return type("A", (), {"dry_run": dry_run, "no_reharden": no_reharden, "json": json_out})()


def _refresh_fixture(tmp_path, monkeypatch, live=None, launcher_stale=None):
    """Pin everything cmd_refresh reads: the steam root/game dir (irrelevant
    once identify() and launcher_is_stale() are stubbed), the live build, and
    whether the hardened launcher is stale."""
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.gamebuild, "identify", lambda game, root: live or build_id())
    monkeypatch.setattr(cli.doctor_mod, "launcher_is_stale", lambda game: launcher_stale)


def _stamp(tmp_path, build):
    state = state_mod.load_state()
    state_mod.record_build(state, build)
    state_mod.write_state(tmp_path / "installed.json", state)


def test_refresh_on_an_unstamped_stack_does_not_claim_the_build_matches(tmp_path, monkeypatch, capsys):
    # No installed.json at all -> stamped_build() is None -> plan_heal(None, ...)
    # plans nothing, which is correct: there is no prior build to have drifted
    # from. But "already built for 1.17.0" claims knowledge nothing has -- the
    # stack could have been built against anything.
    _refresh_fixture(tmp_path, monkeypatch)
    rc = cli.cmd_refresh(_refresh_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "already built for" not in out
    assert "not recorded" in out


def test_refresh_says_nothing_to_do_when_the_stamp_matches(tmp_path, monkeypatch, capsys):
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id())
    rc = cli.cmd_refresh(_refresh_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to do" in out.lower()


def test_refresh_dry_run_prints_the_full_rebase_plan(tmp_path, monkeypatch, capsys):
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                              steam_buildid="1", regulation_sha="b" * 64))
    rc = cli.cmd_refresh(_refresh_args(dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    for kind in ("adopt-baseline", "repin", "gate", "rebuild", "verify", "stamp"):
        assert kind in out
    assert "dry run" in out.lower()


def test_refresh_without_dry_run_points_at_apply(tmp_path, monkeypatch, capsys):
    # `erm refresh` with no flags is the obvious thing to type on a patched
    # install. It should print the plan and name the command that carries it
    # out, rather than refusing with an error about something being unwired.
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                              steam_buildid="1", regulation_sha="b" * 64))
    rc = cli.cmd_refresh(_refresh_args(dry_run=False))
    out = capsys.readouterr().out
    assert rc == 0
    for kind in ("adopt-baseline", "repin", "gate", "rebuild", "verify", "stamp"):
        assert kind in out
    assert "erm apply" in out


def test_refresh_names_the_one_step_apply_does_not_carry_out(tmp_path, monkeypatch, capsys):
    # apply adopts the baseline, gates, rebuilds, verifies and re-stamps -- but
    # it never re-resolves a pin. Sending the user to it for "the plan" promises
    # a step it doesn't run.
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                              steam_buildid="1", regulation_sha="b" * 64))
    rc = cli.cmd_refresh(_refresh_args(dry_run=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "erm apply" in out
    assert "erm update" in out


def test_refresh_on_a_tampered_build_exits_on_the_refusal(tmp_path, monkeypatch, capsys):
    # The plan is one refusal and nothing else, so the refusal IS the outcome.
    # Following it with "executing a heal is not wired up yet" points at the
    # wrong thing -- there is nothing here anyone would want executed.
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id(regulation_sha="b" * 64))
    rc = cli.cmd_refresh(_refresh_args(dry_run=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Verify integrity" in out
    assert "not wired up" not in out


def test_refresh_no_reharden_flag_suppresses_the_reharden_step(tmp_path, monkeypatch, capsys):
    _refresh_fixture(tmp_path, monkeypatch, launcher_stale=("2.6.2.0", "2.7.0.0"))
    _stamp(tmp_path, build_id(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                              steam_buildid="1", regulation_sha="b" * 64))
    cli.cmd_refresh(_refresh_args(dry_run=True, no_reharden=True))
    assert "reharden" not in capsys.readouterr().out
    cli.cmd_refresh(_refresh_args(dry_run=True, no_reharden=False))
    assert "reharden" in capsys.readouterr().out


def test_refresh_dry_run_reports_a_tampered_build_as_a_refusal(tmp_path, monkeypatch, capsys):
    # exe/steam_buildid unchanged, only regulation.bin moved -> tampered, not a
    # real patch. plan_heal must refuse rather than offer adopt-baseline, and
    # cmd_refresh must surface that refusal as a failure, not an info line.
    _refresh_fixture(tmp_path, monkeypatch)
    _stamp(tmp_path, build_id(regulation_sha="b" * 64))
    rc = cli.cmd_refresh(_refresh_args(dry_run=True))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Verify integrity" in out
    assert "adopt-baseline" not in out


def test_refresh_reports_a_stale_launcher_on_an_unstamped_stack(tmp_path, monkeypatch, capsys):
    # refresh already computes launcher_is_stale() and hands it to plan_heal.
    # Dropping it left doctor warning about a launcher a build behind while
    # refresh -- the command whose whole job is "what needs bringing forward"
    # -- said nothing.
    _refresh_fixture(tmp_path, monkeypatch, launcher_stale=("2.6.2.0", "2.7.0.0"))
    rc = cli.cmd_refresh(_refresh_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "reharden" in out
    assert "not recorded" not in out or "reharden" in out


def test_refresh_points_a_launcher_only_plan_at_harden_not_apply(tmp_path, monkeypatch, capsys):
    # apply cannot carry this one out: it only auto-hardens an install that is
    # not already hardened, and the swap is chattr +i. Offering it would send
    # the reader in a circle.
    _refresh_fixture(tmp_path, monkeypatch, launcher_stale=("2.6.2.0", "2.7.0.0"))
    cli.cmd_refresh(_refresh_args())
    out = capsys.readouterr().out
    assert "unharden" in out
    assert "rebase every merge" not in out


def _one_json_document(capsys):
    """Everything a --json run put on stdout, parsed as ONE document.

    `| head -1 | jq .` is not machine-readable output, so this deliberately
    parses the whole stream: a prose tail or a second document raises here.
    """
    return json.loads(capsys.readouterr().out)


def _fake_audit(findings, caveat="cannot certify a save as legitimate"):
    return type("R", (), {"findings": findings, "caveat": caveat})()


def _finding(severity, message, slot=0):
    return type("F", (), {"severity": severity, "slot": slot, "message": message})()


def _stub_save(monkeypatch, findings):
    monkeypatch.setattr(cli, "SaveFile", type("S", (), {"from_bytes": staticmethod(lambda b: None)}))
    monkeypatch.setattr(cli, "audit_save", lambda sf: _fake_audit(findings))


def test_audit_json_is_one_document_carrying_the_caveat(tmp_path, monkeypatch, capsys):
    # The caveat is the whole point of the audit — it must ride inside the
    # document, not as prose after it that makes the stream unparseable.
    save = tmp_path / "ER0000.sl2"
    save.write_bytes(b"\x00")
    _stub_save(monkeypatch, [])

    rc = cli.cmd_audit(type("A", (), {"json": True, "save": str(save)})())
    data = _one_json_document(capsys)

    assert rc == 0
    assert data["worst"] == "ok"
    assert any("certify" in i["message"] for i in data["items"])


def test_audit_exits_nonzero_on_a_decisive_finding(tmp_path, monkeypatch, capsys):
    # A shell gating on `erm audit` must not read a decisively tampered save as
    # a pass. The report already ranks it a failure; the exit code has to agree.
    save = tmp_path / "ER0000.sl2"
    save.write_bytes(b"\x00")
    _stub_save(monkeypatch, [_finding("decisive", "entry USER_DATA_000 MD5 mismatch")])

    rc = cli.cmd_audit(type("A", (), {"json": False, "save": str(save)})())
    out = capsys.readouterr().out

    assert rc == 1
    assert "MD5 mismatch" in out


def test_audit_stays_zero_on_a_suspicious_but_not_decisive_finding(tmp_path, monkeypatch, capsys):
    save = tmp_path / "ER0000.sl2"
    save.write_bytes(b"\x00")
    _stub_save(monkeypatch, [_finding("suspicious", "rune count is high")])

    rc = cli.cmd_audit(type("A", (), {"json": False, "save": str(save)})())
    capsys.readouterr()

    assert rc == 0


def test_apply_json_is_one_document_with_the_doctor_nested(tmp_path, monkeypatch, capsys):
    # apply printed its report, then the literal line "Safety check (erm doctor):",
    # then a SECOND document — three things json.loads can't read as one.
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "start_protected_game.exe").write_bytes(b"\x00")
    _seed_apply_fixture(tmp_path, game_dir)
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_apply(type("A", (), {"profile": "seamless-only", "json": True})())
    data = _one_json_document(capsys)

    assert rc == 0
    # The fake Game/ has no regulation.bin, so the nested doctor cannot identify
    # a build and warns -- which is the proof that it ran the build checks at all.
    assert data["doctor"]["worst"] == "warn"
    assert any("game build" in i["message"] for i in data["doctor"]["items"]), (
        "the nested doctor must run the same checks erm doctor runs")
    assert any("seamless-coop" in i["message"] for i in data["items"])


def test_tidy_dry_run_json_is_one_document_carrying_the_count(tmp_path, monkeypatch, capsys):
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "ersc_logs").mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.setattr(cli.tidy, "find_cruft", lambda game, recorded: [game / "ersc_logs"])
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_tidy(type("A", (), {"json": True, "apply": False})())
    data = _one_json_document(capsys)

    assert rc == 0
    assert any("ersc_logs" in i["message"] for i in data["items"])
    assert any("erm tidy --apply" in i["message"] for i in data["items"])


def test_backup_json_is_a_document_naming_the_snapshot(tmp_path, monkeypatch, capsys):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "ER0000.co2").write_bytes(b"coop")
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_backup(type("A", (), {"json": True, "label": ""})())
    data = _one_json_document(capsys)

    assert rc == 0
    assert any("ER0000.co2" in i["message"] for i in data["items"])


def test_backup_reports_a_failure_when_there_is_no_save(tmp_path, monkeypatch, capsys):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_backup(type("A", (), {"json": True, "label": ""})())
    data = _one_json_document(capsys)

    assert rc == 1
    assert data["worst"] == "fail"


def test_restore_json_is_a_document_naming_the_destination(tmp_path, monkeypatch, capsys):
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "snap.co2").write_bytes(b"snapshot-data")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_restore(type("A", (), {"json": True, "backup": "snap.co2"})())
    data = _one_json_document(capsys)

    assert rc == 0
    assert any("ER0000.co2" in i["message"] for i in data["items"])


def _hardened_game(tmp_path, monkeypatch):
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "eldenring.exe").write_bytes(b"GAME")
    (game_dir / "start_protected_game.exe").write_bytes(b"EAC")
    monkeypatch.setattr(cli.harden, "set_immutable", lambda path, on: None)
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    cli.harden.harden_swap(game_dir)
    return game_dir


def test_unharden_json_is_one_document_with_the_doctor_nested(tmp_path, monkeypatch, capsys):
    _hardened_game(tmp_path, monkeypatch)

    rc = cli.cmd_unharden(type("A", (), {"json": True})())
    data = _one_json_document(capsys)

    assert rc == 0
    # The fake Game/ has no regulation.bin, so the nested doctor cannot identify
    # a build and warns -- which is the proof that it ran the build checks at all.
    assert data["doctor"]["worst"] == "warn"
    assert any("game build" in i["message"] for i in data["doctor"]["items"]), (
        "the nested doctor must run the same checks erm doctor runs")


def test_harden_json_is_one_document_with_the_doctor_nested(tmp_path, monkeypatch, capsys):
    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "eldenring.exe").write_bytes(b"GAME")
    (game_dir / "start_protected_game.exe").write_bytes(b"EAC")
    monkeypatch.setattr(cli.harden, "set_immutable", lambda path, on: None)
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)

    rc = cli.cmd_harden(type("A", (), {"json": True})())
    data = _one_json_document(capsys)

    assert rc == 0
    assert data["doctor"]["worst"] != "fail"


def test_unharden_exit_code_reports_the_install_a_mod_loader_is_left_in(tmp_path, monkeypatch, capsys):
    # After unharden on a proxy-DLL install the real EAC launcher sits next to
    # dinput8.dll — the mixed state doctor fails on. The exit code reports the
    # install's safety, not whether the command itself succeeded, so it must be
    # non-zero even though the restore worked.
    game_dir = _hardened_game(tmp_path, monkeypatch)
    (game_dir / "dinput8.dll").write_bytes(b"\x00")

    rc = cli.cmd_unharden(type("A", (), {"json": False})())
    out = capsys.readouterr().out

    assert rc == 1
    assert "dinput8.dll" in out


def test_json_flag_is_accepted_after_the_subcommand():
    # `erm status --json` is the git/docker-conventional form and exited 2.
    parser = build_parser()
    for cmd in ("doctor", "status", "verify", "quarantine", "tidy"):
        assert parser.parse_args([cmd, "--json"]).json is True


def test_json_flag_still_works_before_the_subcommand():
    # The subparser copy must not carry a store_true default: it would clobber
    # the root's True and silently print prose for the form that worked.
    parser = build_parser()
    assert parser.parse_args(["--json", "doctor"]).json is True
    assert parser.parse_args(["doctor"]).json is False


def test_restore_keeps_the_save_it_is_about_to_overwrite(tmp_path, monkeypatch, capsys):
    # The last copy of the live character. Asserting the ORIGINAL bytes landed
    # in backups/ is what makes this ordering-proof: a backup taken after the
    # copy would hold the snapshot's bytes and pass a mere "a file appeared".
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "snap.co2").write_bytes(b"snapshot-data")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "ER0000.co2").write_bytes(b"live-character")

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.setattr(cli, "_stamp", lambda: "STAMP")
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_restore(type("A", (), {"json": False, "backup": "snap.co2"})())
    capsys.readouterr()

    assert rc == 0
    assert (save_dir / "ER0000.co2").read_bytes() == b"snapshot-data"
    assert (backups_dir / "ER0000.co2.STAMP-pre-restore").read_bytes() == b"live-character"


def test_backup_snapshots_the_coop_save_not_the_vanilla_one(tmp_path, monkeypatch, capsys):
    # Both files sit in the prefix on a seamless-coop install. Steam Cloud does
    # not cover .co2, so backing up the .sl2 instead would leave the only
    # unprotected save with no backup at all.
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "ER0000.sl2").write_bytes(b"vanilla")
    (save_dir / "ER0000.co2").write_bytes(b"coop")

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.setattr(cli, "_stamp", lambda: "STAMP")
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_backup(type("A", (), {"json": False, "label": "preboss"})())
    capsys.readouterr()

    assert rc == 0
    assert (tmp_path / "backups" / "ER0000.co2.STAMP-preboss").read_bytes() == b"coop"


def test_quarantine_forwards_live_steam_state_into_the_refusal(tmp_path, monkeypatch):
    # Steam re-syncs the save from the cloud the moment it notices it gone, so
    # the refusal is the whole safety property. cmd_quarantine is the only
    # thing that reads whether Steam is up.
    from ermlib.errors import SafetyError

    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "ER0000.sl2").write_bytes(b"vanilla")
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.setattr(cli.steam, "cloud_saves", lambda root: [])
    monkeypatch.setattr(cli.steam, "steam_running", lambda: True)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SafetyError):
        cli.cmd_quarantine(type("A", (), {"json": False})())
    assert (save_dir / "ER0000.sl2").exists()      # nothing moved on the refusal


def test_bare_erm_prints_help_and_exits_two(capsys):
    # `python3 erm` with no subcommand. The usage block is also the only place
    # that documents where --json goes.
    rc = main([])
    out = capsys.readouterr().out

    assert rc == 2
    assert "usage:" in out


def test_an_ermerror_becomes_a_clean_stderr_line_and_exit_one(capsys):
    # The one place in the tree that turns any of the ErmError family into user
    # output. Narrowing the except, dropping file=sys.stderr, or returning 0
    # here would replace every clean error message with a traceback.
    rc = main(["audit", "/nonexistent/ER0000.sl2"])
    cap = capsys.readouterr()

    assert rc == 1
    assert cap.err.startswith("error: ")
    assert "cannot read save" in cap.err
    assert cap.out == ""


def test_a_command_returning_none_exits_zero(monkeypatch):
    # No shipping command returns None, so pin the coercion as a contract
    # instead. Patching before the call works because main() rebuilds the
    # parser every time, so set_defaults(func=...) picks up the patched global.
    monkeypatch.setattr(cli, "cmd_audit", lambda args: None)
    assert main(["audit", "whatever"]) == 0


def test_refresh_still_reports_a_stale_launcher_under_no_reharden(tmp_path, monkeypatch, capsys):
    # --no-reharden only decides whether the re-copy is part of the printed
    # plan. Letting it also drop the FINDING put refresh back to printing a
    # green all-clear over a start_protected_game.exe a build behind the
    # eldenring.exe Steam actually runs.
    _refresh_fixture(tmp_path, monkeypatch, launcher_stale=("2.6.2.0", "2.7.0.0"))
    _stamp(tmp_path, build_id())

    rc = cli.cmd_refresh(_refresh_args(no_reharden=True))
    out = capsys.readouterr().out

    assert rc == 0
    assert "2.6.2.0" in out and "2.7.0.0" in out
    assert "nothing to do" not in out.lower()


def test_refresh_help_promises_neither_a_change_nor_a_sudo_prompt(capsys):
    # refresh only reports — it never writes to the install and cannot reach
    # the one call that prompts for sudo. Its two option lines said otherwise,
    # and they are the whole screen `erm refresh --help` prints.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["refresh", "--help"])
    out = capsys.readouterr().out

    assert "sudo" not in out
    assert "without changing anything" not in out


def test_no_module_reaches_into_another_modules_privates():
    """Underscore names belong to their own module.

    cli used to do its one unverified download through `github._fetch_bytes`,
    the raw transport behind a door marked private — so github's public surface
    said nothing about who depended on it. Anything a sibling needs gets a
    public name.
    """
    import ast

    ermlib_dir = pathlib.Path(cli.__file__).resolve().parent
    # namedtuple's own API is spelled with a leading underscore; it isn't a
    # module private and there's no public alternative to reach for.
    namedtuple_api = {"_replace", "_asdict", "_fields", "_make", "_field_defaults"}
    reaches = []
    for src in sorted(ermlib_dir.rglob("*.py")):
        tree = ast.parse(src.read_text())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                modules.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.Import):
                modules.update((a.asname or a.name).split(".")[0] for a in node.names)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in modules
                    and node.attr.startswith("_")
                    and node.attr not in namedtuple_api):
                reaches.append(f"{src.name}:{node.lineno} {node.value.id}.{node.attr}")
    assert reaches == []


def test_an_old_interpreter_gets_a_sentence_not_a_traceback():
    # ermlib.manifest imports tomllib, which is 3.11+. Below that every
    # invocation — `erm --version` included — died with a raw
    # ModuleNotFoundError traceback from an import three modules deep.
    assert _erm.unsupported_python((3, 10, 12)) != ""
    assert _erm.unsupported_python((3, 9, 2)).startswith("error:")
    # Both the floor and what's actually running, so the reader knows which
    # python3 on their box is the problem.
    assert "3.11" in _erm.unsupported_python((3, 10, 12))
    assert "3.10" in _erm.unsupported_python((3, 10, 12))
    assert _erm.unsupported_python((3, 11, 0)) == ""
    assert _erm.unsupported_python((3, 14, 0)) == ""


def test_the_version_guard_runs_before_ermlib_is_imported():
    # The guard is only worth anything if it runs FIRST: importing ermlib is
    # what raises ModuleNotFoundError on an old interpreter, so an import
    # hoisted above the guard puts the traceback back.
    import ast

    body = ast.parse(_ERM.read_text()).body
    ermlib_import = next(i for i, node in enumerate(body)
                         if isinstance(node, ast.ImportFrom)
                         and (node.module or "").startswith("ermlib"))
    before = body[:ermlib_import]
    assert any(isinstance(node, ast.Assign) and "unsupported_python" in ast.unparse(node)
               for node in before)
    assert any(isinstance(node, ast.If) and "sys.exit" in ast.unparse(node)
               for node in before)


def _save_dirs(tmp_path, monkeypatch):
    """A cwd with backups/ beside it and a save dir the commands write into."""
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)
    return save_dir


def test_backups_lists_the_names_restore_takes(tmp_path, monkeypatch, capsys):
    # `erm restore` takes a name out of backups/, and nothing printed one — so
    # restoring meant already knowing a timestamped filename. The quarantined
    # vanilla save is the one people most need, and it sits a level down.
    _save_dirs(tmp_path, monkeypatch)
    backups = tmp_path / "backups"
    (backups / "quarantine").mkdir(parents=True)
    (backups / "ER0000.co2.20260902-141530").write_bytes(b"snapshot")
    (backups / "quarantine" / "ER0000.sl2.20260902-141530").write_bytes(b"vanilla")

    rc = cli.cmd_backups(type("A", (), {"json": False})())

    out = capsys.readouterr().out
    assert rc == 0
    assert "ER0000.co2.20260902-141530" in out
    # Printed relative to backups/, because that's what restore resolves.
    assert "quarantine/ER0000.sl2.20260902-141530" in out


def test_backups_says_so_when_there_are_none(tmp_path, monkeypatch, capsys):
    _save_dirs(tmp_path, monkeypatch)
    rc = cli.cmd_backups(type("A", (), {"json": False})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "backup" in out.lower()


def test_backups_is_a_registered_subcommand():
    args = build_parser().parse_args(["backups"])
    assert args.func is cli.cmd_backups


def test_restore_with_an_unknown_name_names_the_ones_that_exist(tmp_path, monkeypatch):
    # A typo used to snapshot the live save FIRST and only then discover the
    # source doesn't exist, dropping another -pre-restore file into a directory
    # nothing could list.
    save_dir = _save_dirs(tmp_path, monkeypatch)
    (save_dir / "ER0000.co2").write_bytes(b"live-character")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "ER0000.co2.20260902-141530").write_bytes(b"snapshot")

    with pytest.raises(PathError) as exc:
        cli.cmd_restore(type("A", (), {"backup": "ER0000.co2.20260902-14153"})())

    assert "ER0000.co2.20260902-141530" in str(exc.value)
    assert [p.name for p in backups.iterdir()] == ["ER0000.co2.20260902-141530"]
    assert (save_dir / "ER0000.co2").read_bytes() == b"live-character"
