from ermlib.report import Report
from ermlib.doctor import scan_game_dir, run_doctor, eac_state
from ermlib import harden


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
