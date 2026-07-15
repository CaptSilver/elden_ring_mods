import os
import pytest
from ermlib.paths import (
    same_location,
    find_game_dir,
    find_prefix,
    find_save_dir,
    APPID,
)
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


def test_find_prefix_locates_and_raises(tmp_path):
    pfx = tmp_path / "steamapps" / "compatdata" / APPID / "pfx"
    pfx.mkdir(parents=True)
    assert find_prefix(tmp_path).samefile(pfx)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PathError):
        find_prefix(empty)


def test_find_save_dir_finds_steamid_subfolder(tmp_path):
    steamid = "76561198000000000"
    save_dir = (
        tmp_path
        / "steamapps"
        / "compatdata"
        / APPID
        / "pfx"
        / "drive_c/users/steamuser/AppData/Roaming/EldenRing"
        / steamid
    )
    save_dir.mkdir(parents=True)
    (save_dir / "ER0000.sl2").write_bytes(b"save")
    assert find_save_dir(tmp_path).samefile(save_dir)


def test_find_save_dir_raises_without_save(tmp_path):
    # Prefix exists but no ER0000.sl2 anywhere -> PathError, not empty return.
    (tmp_path / "steamapps" / "compatdata" / APPID / "pfx").mkdir(parents=True)
    with pytest.raises(PathError):
        find_save_dir(tmp_path)


def test_find_game_dir_finds_game_in_secondary_library(tmp_path):
    # Primary library holds no game; game lives in a SECONDARY library that only
    # libraryfolders.vdf points at. Exercises the multi-library VDF parse.
    steam_root = tmp_path / "primary"
    (steam_root / "steamapps").mkdir(parents=True)

    lib2 = tmp_path / "lib2"
    game = lib2 / "steamapps" / "common" / "ELDEN RING" / "Game"
    game.mkdir(parents=True)

    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    vdf.write_text(
        '"libraryfolders"\n'
        "{\n"
        '\t"0"\n'
        "\t{\n"
        f'\t\t"path"\t\t"{lib2}"\n'
        "\t}\n"
        "}\n"
    )
    assert find_game_dir(steam_root).samefile(game)


def test_find_save_dir_skips_unreadable_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions; can't simulate EACCES")
    root = (
        tmp_path
        / "steamapps"
        / "compatdata"
        / APPID
        / "pfx"
        / "drive_c/users/steamuser/AppData/Roaming/EldenRing"
    )
    root.mkdir(parents=True)
    os.chmod(root, 0o000)
    try:
        # iterdir() on the unreadable root raises PermissionError internally;
        # discovery must swallow it and raise PathError, never leak OSError.
        with pytest.raises(PathError):
            find_save_dir(tmp_path)
    finally:
        os.chmod(root, 0o755)
