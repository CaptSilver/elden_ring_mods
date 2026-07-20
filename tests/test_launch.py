from pathlib import Path

from ermlib import launch


def test_repo_root_holds_the_me3_profile():
    # Derived from __file__, not cwd, so the printed command is right no matter
    # where erm was invoked from.
    assert (launch.REPO_ROOT / "ermlib" / "launch.py").exists()
    assert launch.PROFILE == launch.REPO_ROOT / "tools" / "me3" / "erm-coop.me3"
    assert launch.PROFILE.is_absolute()


def test_find_me3_prefers_path_lookup(monkeypatch, tmp_path):
    onpath = tmp_path / "me3"
    onpath.write_text("")
    monkeypatch.setattr(launch.shutil, "which", lambda n: str(onpath))
    assert launch.find_me3() == onpath.resolve()


def test_find_me3_falls_back_to_local_bin(monkeypatch, tmp_path):
    fallback = tmp_path / "me3"
    fallback.write_text("")
    monkeypatch.setattr(launch.shutil, "which", lambda n: None)
    monkeypatch.setattr(launch, "ME3_FALLBACK", fallback)
    assert launch.find_me3() == fallback.resolve()


def test_find_me3_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda n: None)
    monkeypatch.setattr(launch, "ME3_FALLBACK", tmp_path / "nope")
    assert launch.find_me3() is None


def test_me3_command_is_absolute_and_ends_with_command_token(tmp_path):
    cmd = launch.me3_command(Path("/opt/me3"), tmp_path / "p.me3")
    assert cmd.startswith("/opt/me3 launch -p /")
    # Steam appends this text as argv to the game exe unless %command% appears.
    assert cmd.endswith("# %command%")


def test_me3_command_quotes_paths_containing_spaces(tmp_path):
    weird = tmp_path / "a b" / "erm-coop.me3"
    cmd = launch.me3_command(Path("/opt/me3"), weird)
    assert f"'{weird}'" in cmd
