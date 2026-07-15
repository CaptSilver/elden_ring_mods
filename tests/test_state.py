from ermlib.state import load_state, record_install, write_state, forget


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
