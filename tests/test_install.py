import zipfile
from pathlib import Path

import pytest

from ermlib.errors import ErmError
from ermlib.install import apply_ersc, extract_archive, inject_password, read_secret


def _make_ersc_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def test_apply_ersc_extracts_and_sets_password(tmp_path, tmp_game):
    z = tmp_path / "ersc.zip"
    _make_ersc_zip(z)
    files = apply_ersc(z, tmp_game, password="hunter2")
    assert (tmp_game / "ersc_launcher.exe").exists()
    ini = (tmp_game / "SeamlessCoop" / "ersc_settings.ini").read_text()
    assert "cooppassword = hunter2" in ini
    assert "save_file_extension = co2" in ini    # never clobbered
    # the returned file list is what `erm uninstall` later relies on to know
    # exactly what to remove
    assert "ersc_launcher.exe" in files
    assert "SeamlessCoop/ersc_settings.ini" in files


def test_apply_ersc_rejects_traversal_archive(tmp_path, tmp_game):
    # A trojaned mod archive with a zip-slip entry must be rejected outright,
    # before any extraction — the sha256 pin proves it's the chosen file, not
    # that it's benign.
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ersc_launcher.exe", b"\x00")
        zf.writestr("../evil.txt", b"pwned")
    with pytest.raises(ErmError):
        apply_ersc(z, tmp_game, password="x")
    # nothing extracted outside the game dir (parent stays clean) and the
    # archive was refused whole — not partially extracted.
    assert not (tmp_path / "evil.txt").exists()


def test_read_secret(tmp_path):
    env = tmp_path / "secrets.env"
    env.write_text("COOP_PASSWORD=swordfish\n")
    assert read_secret(env) == "swordfish"


def _make_bare_dll_zip(path, member="y.dll"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, b"\x00")


def test_extract_archive_to_game_root_returns_bare_relative_paths(tmp_path, tmp_game):
    # A techiew-style mod archive that already ships its own "mods/" folder
    # gets extracted straight into Game/ (subdir="") — the archive's own
    # layout puts the file at the right spot.
    z = tmp_path / "mod-a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("mods/x.dll", b"\x00")
    files = extract_archive(z, tmp_game, "")
    assert (tmp_game / "mods" / "x.dll").exists()
    assert files == ["mods/x.dll"]


def test_extract_archive_to_mods_subdir_prefixes_returned_paths(tmp_path, tmp_game):
    # A bare-DLL mod archive (no internal mods/ folder) installs into
    # Game/mods/ — extract_archive must prefix the returned paths with the
    # subdir so installed.json (and later uninstall) sees the real
    # game-relative location, not just the name inside the zip.
    z = tmp_path / "mod-b.zip"
    _make_bare_dll_zip(z, "y.dll")
    files = extract_archive(z, tmp_game, "mods")
    assert (tmp_game / "mods" / "y.dll").exists()
    assert files == ["mods/y.dll"]


def test_extract_archive_rejects_traversal_before_extracting(tmp_path, tmp_game):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("y.dll", b"\x00")
        zf.writestr("../evil.txt", b"pwned")
    with pytest.raises(ErmError):
        extract_archive(z, tmp_game, "mods")
    # refused whole, not partially extracted
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_game / "mods").exists()
