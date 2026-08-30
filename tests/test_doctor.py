import struct
import zipfile
from pathlib import Path

from ermlib.report import Report
from ermlib.doctor import scan_game_dir, run_doctor, eac_state
from ermlib import doctor, harden
from ermlib.formats import regulation
from ermlib.gamebuild import BuildId


def _fake_exe(major, minor, patch, build):
    ms = (major << 16) | minor
    ls = (patch << 16) | build
    return (b"MZ" + b"\x00" * 64 + struct.pack("<I", 0xFEEF04BD)
            + struct.pack("<I", 0x00010000) + struct.pack("<II", ms, ls)
            + b"\x00" * 32)


def _bid(**over):
    base = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
                steam_buildid="23850278", regulation_sha="a" * 64)
    base.update(over)
    return BuildId(**base)


def test_scan_flags_proxy_and_modengine(tmp_game):
    (tmp_game / "dinput8.dll").write_bytes(b"\x00")
    (tmp_game / "modengine.toml").write_text("x")
    found = scan_game_dir(tmp_game)
    assert "dinput8.dll" in found
    assert "modengine.toml" in found


def test_doctor_fails_on_disarmed_eac_with_proxy(tmp_game):
    # The proxy DLL flips eac_state to "disarmed" (a proxy alongside
    # start_protected_game.exe makes EAC not cleanly load). start_protected_game.exe
    # still exists, so a vanilla online launch is still possible, and the proxy is
    # exactly how the mod loads unnoticed. disarmed + a forbidden artifact is the
    # dangerous mixed state -> fail. Do NOT read "armed" here: with a proxy present
    # eac_state is "disarmed", and gating fail on state == "armed" reintroduces the
    # original bug.
    (tmp_game / "dinput8.dll").write_bytes(b"\x00")
    r = run_doctor(tmp_game, Report())
    assert r.worst_level == "fail"


def test_doctor_warns_not_fails_when_vanilla_unlaunchable(tmp_game):
    # exe-swap scenario: forbidden artifacts present but no start_protected_game.exe,
    # so eac_state is "absent". A vanilla online launch is impossible, so there is no
    # ban path -> warn, never fail. Pins the absent + forbidden -> warn boundary.
    (tmp_game / "start_protected_game.exe").unlink()
    (tmp_game / "dinput8.dll").write_bytes(b"\x00")
    (tmp_game / "modengine.toml").write_text("x")
    r = run_doctor(tmp_game, Report())
    assert r.worst_level != "fail"


def test_doctor_fails_on_armed_plus_nonproxy_forbidden(tmp_game):
    # start_protected_game.exe present with NO proxy DLL -> eac_state "armed".
    # A forbidden non-proxy artifact (mod/regulation.bin) is still the dangerous
    # mixed state -> fail. Pins the pure-armed + forbidden -> fail path (the
    # disarmed sibling is covered above).
    mod = tmp_game / "mod"
    mod.mkdir()
    (mod / "regulation.bin").write_bytes(b"\x00")
    r = run_doctor(tmp_game, Report())
    assert r.worst_level == "fail"


def test_doctor_clean_when_ersc_only(tmp_game):
    (tmp_game / "SeamlessCoop").mkdir()
    (tmp_game / "ersc_launcher.exe").write_bytes(b"\x00")
    r = run_doctor(tmp_game, Report())
    assert r.worst_level in ("ok", "warn")


def test_doctor_warns_on_spawner_regardless_of_name_case(tmp_game):
    # Windows filenames are case-preserving but case-insensitive; a spawner
    # DLL with mixed-case name/extension must still be caught.
    (tmp_game / "Glorious_Merchant.DLL").write_bytes(b"\x00")
    r = run_doctor(tmp_game, Report())
    assert r.worst_level == "warn"
    assert any("spawner" in msg.lower() for _, msg in r.items)


def test_eac_state_reports_hardened_when_backup_exists(tmp_game):
    # After harden_swap, start_protected_game.exe exists (it's the eldenring
    # copy), so without this check eac_state would misread it as "armed".
    # is_hardened (backup present) must win over the exe-presence check.
    harden.harden_swap(tmp_game)
    assert eac_state(tmp_game) == "hardened"


def test_doctor_reports_hardened_as_safe_not_fail(tmp_game):
    harden.harden_swap(tmp_game)
    r = run_doctor(tmp_game, Report())
    assert r.worst_level != "fail"
    assert any("hardened" in msg.lower() for _, msg in r.items)


def test_stale_hardened_launcher_is_detected_by_version(tmp_game):
    # The exact skew found on this machine: Steam patched eldenring.exe and
    # left the swapped launcher on the old build.
    (tmp_game / "start_protected_game.exe.erm-backup").write_bytes(b"\x00")
    (tmp_game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (tmp_game / "start_protected_game.exe").write_bytes(_fake_exe(2, 6, 2, 0))
    assert doctor.launcher_is_stale(tmp_game) == ("2.6.2.0", "2.7.0.0")


def test_a_current_swap_is_not_stale(tmp_game):
    (tmp_game / "start_protected_game.exe.erm-backup").write_bytes(b"\x00")
    (tmp_game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (tmp_game / "start_protected_game.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    assert doctor.launcher_is_stale(tmp_game) is None


def test_an_unswapped_launcher_is_not_reported_as_stale(tmp_game):
    # The real EAC launcher is a different product (1.9.4.0) and is SUPPOSED to
    # differ from eldenring.exe. Only a hardened install can be stale — do NOT
    # create the erm-backup file here, or is_hardened() would be True and this
    # test would compare two unrelated products' versions.
    (tmp_game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (tmp_game / "start_protected_game.exe").write_bytes(_fake_exe(1, 9, 4, 0))
    assert doctor.launcher_is_stale(tmp_game) is None


def _regulation_blob(version):
    """A real regulation.bin -- encrypted and compressed -- carrying `version`.

    Small enough to build per test, and round-trips through the same reader
    production uses, so these tests exercise the file rather than a stub.
    """
    payload = bytearray(0x20)
    payload[0:4] = b"BND4"
    payload[0x18:0x20] = version.encode()
    return regulation.pack(bytes(payload), bytes(16))


def _merged_state(tmp_path, version):
    package = tmp_path / "tools" / "me3" / "mods" / "_merged"
    package.mkdir(parents=True)
    (package / "regulation.bin").write_bytes(_regulation_blob(version))
    return {"_merged": {"kind": "me3-package", "package": str(package),
                        "paths": {"regulation.bin": ["clevers-moveset"]}}}


def test_a_merged_regulation_from_an_older_build_is_warned(tmp_game, tmp_path):
    # The stamp says apply ran against 1.17, and it did -- but the merged file
    # it left behind is still made of 1.16 game data. Stamp and artifact are
    # different claims and only the artifact is checkable.
    state = _merged_state(tmp_path, "11601000")
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state=state)
    assert r.worst_level == "warn"
    assert any("11601000" in m and "11701000" in m for _, m in r.items)


def test_a_merged_regulation_on_the_installed_build_is_not_warned(tmp_game, tmp_path):
    state = _merged_state(tmp_path, "11701000")
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state=state)
    assert r.worst_level == "ok"


def test_an_unreadable_merged_regulation_is_reported_not_skipped(tmp_game, tmp_path):
    state = _merged_state(tmp_path, "11701000")
    package = Path(state["_merged"]["package"])
    (package / "regulation.bin").write_bytes(b"not a regulation")
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state=state)
    assert r.worst_level == "warn"
    assert any("merged regulation.bin" in m for _, m in r.items)


def test_no_recorded_merge_means_nothing_to_say_about_one(tmp_game):
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state={})
    assert r.worst_level == "ok"
    assert not any("merged regulation.bin" in m for _, m in r.items)


def _vanilla_setup(tmp_path, version):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "rando.zip", "w") as z:
        z.writestr("Vanilla/regulation.bin", _regulation_blob(version))
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "extras.toml").write_text(
        '[[merges]]\n'
        'path = "regulation.bin"\n'
        'strategy = "param-rows"\n'
        'mods = ["a", "b"]\n'
        'prefer = "a"\n'
        'vanilla = { mod = "rando", member = "Vanilla/regulation.bin" }\n')
    return vendor, profiles, {"rando": {"asset": "rando.zip"}}


def test_a_merge_ancestor_from_another_build_is_warned(tmp_game, tmp_path):
    # The mods branched from 1.16, so every merge depends on the fold onto the
    # game's own regulation -- and on their param layouts still fitting it.
    vendor, profiles, lock = _vanilla_setup(tmp_path, "11601000")
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state={},
                                lock=lock, profiles_base=profiles, vendor=vendor)
    assert r.worst_level == "warn"
    assert any("11601000" in m and "ancestor" in m for _, m in r.items)


def test_a_merge_ancestor_on_the_installed_build_is_not_warned(tmp_game, tmp_path):
    vendor, profiles, lock = _vanilla_setup(tmp_path, "11701000")
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report(), state={},
                                lock=lock, profiles_base=profiles, vendor=vendor)
    assert r.worst_level == "ok"


def test_a_repackaged_build_names_the_fields_that_actually_moved(tmp_game):
    # Steam re-packaged the depot: the exe and build id moved, the game data
    # did not. Reporting only the app version says "1.17.0, game is 1.17.0".
    stamped = _bid(exe="2.7.0.1", steam_buildid="1")
    r = doctor.run_build_checks(tmp_game, stamped, _bid(), Report())
    assert r.worst_level == "warn"
    drift = [m for _, m in r.items if "drift" in m][0]
    assert "exe" in drift and "steam_buildid" in drift
    assert "2.7.0.1" in drift and "23850278" in drift
    assert "1.17.0, game is 1.17.0" not in drift


def test_build_drift_is_reported_as_a_warning(tmp_game):
    r = doctor.run_build_checks(tmp_game, _bid(app="1.16.0", regulation="11601000",
                                               exe="2.6.2.0", steam_buildid="1",
                                               regulation_sha="b" * 64),
                                _bid(), Report())
    assert r.worst_level == "warn"
    assert any("1.16.0" in m and "1.17.0" in m for _, m in r.items)


def test_no_drift_reports_the_build_and_stays_ok(tmp_game):
    r = doctor.run_build_checks(tmp_game, _bid(), _bid(), Report())
    assert r.worst_level == "ok"
    assert any("1.17.0" in m for _, m in r.items)


def test_an_unstamped_stack_is_reported_not_warned(tmp_game):
    r = doctor.run_build_checks(tmp_game, None, _bid(), Report())
    assert r.worst_level == "ok"


def test_reports_a_host_me3_older_than_the_pinned_one(tmp_game, monkeypatch):
    # erm fetches me3 but the binary Steam actually launches lives in ~/.local/bin
    # and is installed separately, so it drifts silently. Nothing else notices:
    # the merged artifacts and the build stamp all look correct while the loader
    # running them is two releases behind.
    monkeypatch.setattr(doctor, "installed_me3_version", lambda: "0.11.0")
    r = Report()
    doctor.run_build_checks(tmp_game, None, _bid(), r,
                            lock={"me3": {"version": "v0.13.0"}})
    text = r.render()
    assert "me3" in text and "0.11.0" in text and "0.13.0" in text


def test_says_nothing_when_the_host_me3_matches_the_pin(tmp_game, monkeypatch):
    monkeypatch.setattr(doctor, "installed_me3_version", lambda: "0.13.0")
    r = Report()
    doctor.run_build_checks(tmp_game, None, _bid(), r,
                            lock={"me3": {"version": "v0.13.0"}})
    assert "me3" not in r.render()


def test_stays_quiet_when_no_me3_is_installed(tmp_game, monkeypatch):
    # Not every profile launches through me3; absence is not staleness.
    monkeypatch.setattr(doctor, "installed_me3_version", lambda: None)
    r = Report()
    doctor.run_build_checks(tmp_game, None, _bid(), r,
                            lock={"me3": {"version": "v0.13.0"}})
    assert "me3" not in r.render()
