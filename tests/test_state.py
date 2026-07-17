import pytest

from ermlib import state as state_mod
from ermlib.state import load_state, record_install, write_state, forget
from ermlib.errors import ErmError


def test_record_load_write_round_trip(tmp_path):
    path = tmp_path / "installed.json"
    state = load_state(path)
    assert state == {}          # absent file -> {}

    record_install(state, "seamless-coop", "1.9.9", "seamless-coop-1.9.9.zip",
                    ["ersc_launcher.exe", "SeamlessCoop/ersc_settings.ini"])
    write_state(path, state)

    reloaded = load_state(path)
    assert reloaded == {
        "seamless-coop": {
            "version": "1.9.9",
            "archive": "seamless-coop-1.9.9.zip",
            "files": ["ersc_launcher.exe", "SeamlessCoop/ersc_settings.ini"],
        }
    }


def test_load_state_corrupt_json_raises_ermerror(tmp_path):
    # A truncated/garbage installed.json must surface as a clean ErmError the
    # CLI can print, not a raw JSONDecodeError traceback.
    path = tmp_path / "installed.json"
    path.write_text("{not valid json")
    with pytest.raises(ErmError):
        load_state(path)


def test_forget_removes_entry(tmp_path):
    path = tmp_path / "installed.json"
    state = {}
    record_install(state, "seamless-coop", "1.9.9", "seamless-coop-1.9.9.zip", ["a.exe"])
    write_state(path, state)

    reloaded = load_state(path)
    forget(reloaded, "seamless-coop")
    assert "seamless-coop" not in reloaded
    write_state(path, reloaded)
    assert load_state(path) == {}


def test_record_me3_package_tags_kind_and_path():
    s = {}
    state_mod.record_me3_package(s, "minimal-hud", "1.0", "MinimalHUD.zip", "tools/me3/mods/minimal-hud")
    assert s["minimal-hud"]["kind"] == "me3-package"
    assert s["minimal-hud"]["package"] == "tools/me3/mods/minimal-hud"
    assert s["minimal-hud"]["version"] == "1.0"
    assert s["minimal-hud"]["archive"] == "MinimalHUD.zip"


def test_me3_packages_returns_sorted_id_path_pairs():
    s = {}
    state_mod.record_me3_package(s, "zebra", "1", "z.zip", "tools/me3/mods/zebra")
    state_mod.record_me3_package(s, "alpha", "1", "a.zip", "tools/me3/mods/alpha")
    s["seamless-coop"] = {"version": "1.9.9", "archive": "e.zip", "files": ["x"]}  # a Game/ mod
    assert state_mod.me3_packages(s) == [
        ("alpha", "tools/me3/mods/alpha"),
        ("zebra", "tools/me3/mods/zebra"),
    ]
    assert state_mod.has_me3_packages(s) is True


def test_has_me3_packages_false_without_any():
    assert state_mod.has_me3_packages({"seamless-coop": {"files": []}}) is False
