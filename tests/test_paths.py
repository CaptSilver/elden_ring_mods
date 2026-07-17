import os
import pytest
from ermlib.paths import (
    same_location,
    find_game_dir,
    find_prefix,
    find_save_dir,
    find_proton,
    find_compatdata,
    is_safe_relpath,
    reshade_active,
    APPID,
)


def test_reshade_active_true_for_reshade_symlink(tmp_path):
    # reshade-steam-proton installs ReShade as Game/dxgi.dll -> .../ReShade64.dll
    game = tmp_path / "Game"
    game.mkdir()
    real = tmp_path / "reshade" / "ReShade64.dll"
    real.parent.mkdir()
    real.write_bytes(b"MZ")
    (game / "dxgi.dll").symlink_to(real)
    assert reshade_active(game) is True


def test_reshade_active_false_without_the_symlink(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    # no dxgi.dll at all
    assert reshade_active(game) is False
    # a plain (non-symlink) dxgi.dll is not a ReShade install we placed
    (game / "dxgi.dll").write_bytes(b"MZ")
    assert reshade_active(game) is False


def test_reshade_active_false_for_non_reshade_symlink(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    other = tmp_path / "dxvk" / "dxgi.dll"
    other.parent.mkdir()
    other.write_bytes(b"MZ")
    (game / "dxgi.dll").symlink_to(other)   # points at DXVK, not ReShade
    assert reshade_active(game) is False
from ermlib.errors import PathError


def test_appid_constant():
    assert APPID == "1245620"


def test_is_safe_relpath():
    assert is_safe_relpath("SeamlessCoop/ersc.dll") is True
    assert is_safe_relpath("ersc_launcher.exe") is True
    assert is_safe_relpath("/etc/x") is False        # absolute
    assert is_safe_relpath("../x") is False           # leading traversal
    assert is_safe_relpath("a/../../x") is False       # embedded traversal


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


def test_find_save_dir_finds_co2_only_save(tmp_path):
    # After `erm quarantine` moves ER0000.sl2 out, Seamless Co-op writes
    # ER0000.co2 in its place — find_save_dir must still locate the folder.
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
    (save_dir / "ER0000.co2").write_bytes(b"save")
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


def test_find_proton_prefers_ge_proton_over_plain(tmp_path, monkeypatch):
    # A real steam install can have both a stock "Proton 9.0" and a
    # GE-Proton build under compatibilitytools.d; the GE build is the one
    # that actually runs Windows .exe tools reliably, so it must win even
    # though it's not first alphabetically.
    monkeypatch.setenv("HOME", str(tmp_path))
    tools_dir = tmp_path / ".steam" / "root" / "compatibilitytools.d"
    plain = tools_dir / "Proton 9.0"
    plain.mkdir(parents=True)
    (plain / "proton").write_bytes(b"\x00")
    ge = tools_dir / "GE-Proton10-31"
    ge.mkdir(parents=True)
    (ge / "proton").write_bytes(b"\x00")

    found = find_proton()

    assert found is not None
    assert found.parent.name == "GE-Proton10-31"


def test_find_proton_picks_highest_ge_proton_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tools_dir = tmp_path / ".steam" / "root" / "compatibilitytools.d"
    for name in ("GE-Proton9-20", "GE-Proton10-31", "GE-Proton10-2"):
        d = tools_dir / name
        d.mkdir(parents=True)
        (d / "proton").write_bytes(b"\x00")

    found = find_proton()

    assert found.parent.name == "GE-Proton10-31"


def test_find_proton_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_proton() is None


def test_find_compatdata_locates_and_raises(tmp_path):
    cd = tmp_path / "steamapps" / "compatdata" / APPID
    cd.mkdir(parents=True)
    assert find_compatdata(tmp_path).samefile(cd)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PathError):
        find_compatdata(empty)


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
