from ermlib.report import Report
from ermlib.doctor import scan_game_dir, run_doctor


def test_scan_flags_proxy_and_modengine(tmp_game):
    (tmp_game / "dinput8.dll").write_bytes(b"\x00")
    (tmp_game / "modengine.toml").write_text("x")
    found = scan_game_dir(tmp_game)
    assert "dinput8.dll" in found
    assert "modengine.toml" in found


def test_doctor_fails_on_armed_eac_with_proxy(tmp_game):
    # start_protected_game.exe present (armed) AND a proxy dll -> dangerous mixed state
    (tmp_game / "dinput8.dll").write_bytes(b"\x00")
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
