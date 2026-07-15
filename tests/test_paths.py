import os
import pytest
from ermlib.paths import same_location, find_game_dir, APPID
from ermlib.errors import PathError


def test_appid_constant():
    assert APPID == "1245620"


def test_same_location_uses_inode(tmp_path):
    a = tmp_path / "a"
    a.write_text("x")
    link = tmp_path / "b"
    os.symlink(a, link)
    assert same_location(a, link) is True
    c = tmp_path / "c"
    c.write_text("x")
    assert same_location(a, c) is False


def test_find_game_dir_raises_when_missing(tmp_path):
    with pytest.raises(PathError):
        find_game_dir(tmp_path)


def test_find_game_dir_locates_case_and_space(tmp_path):
    g = tmp_path / "steamapps" / "common" / "ELDEN RING" / "Game"
    g.mkdir(parents=True)
    assert find_game_dir(tmp_path).samefile(g)
