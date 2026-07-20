import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from ermlib import install
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


def _rar_available():
    return shutil.which("bsdtar") is not None


def _make_rar(path, **members):
    """Build a real .rar via bsdtar if it can write one, else skip the test.

    bsdtar can always READ rar; writing depends on the build, so fall back to
    a non-zip format bsdtar definitely writes (7zip) — the code path under test
    is "not a zip, hand it to bsdtar", which either exercises identically.
    """
    src = path.parent / "rarsrc"
    for name, data in members.items():
        p = src / name.replace("|", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    subprocess.run(["bsdtar", "-a", "-cf", str(path), "-C", str(src)]
                   + [n.replace("|", "/") for n in members],
                   check=True, capture_output=True)
    return path


@pytest.mark.skipif(not _rar_available(), reason="bsdtar not installed")
def test_extract_archive_handles_a_non_zip_archive(tmp_path):
    # Nexus serves plenty of mods as .rar/.7z; zipfile can't read them and the
    # mod silently fails to install.
    arc = _make_rar(tmp_path / "mod.7z", **{"QuestPath|QuestPath.dll": b"MZ",
                                            "QuestPath|QuestPath.ini": b"[overlay]"})
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "")
    assert (game / "QuestPath" / "QuestPath.dll").read_bytes() == b"MZ"
    assert sorted(files) == ["QuestPath/QuestPath.dll", "QuestPath/QuestPath.ini"]


@pytest.mark.skipif(not _rar_available(), reason="bsdtar not installed")
def test_extract_archive_non_zip_honours_subdir(tmp_path):
    arc = _make_rar(tmp_path / "mod.7z", **{"Foo.dll": b"MZ"})
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "mods")
    assert (game / "mods" / "Foo.dll").exists()
    assert files == ["mods/Foo.dll"]


@pytest.mark.skipif(not _rar_available(), reason="bsdtar not installed")
def test_extract_archive_non_zip_refuses_traversal(tmp_path, monkeypatch):
    # The zip-slip guard has to cover the bsdtar path too, and reject BEFORE
    # writing anything — a sha256 pin proves which archive, not that it's benign.
    arc = tmp_path / "evil.7z"
    monkeypatch.setattr(install, "_list_archive",
                        lambda p: ["../../escape.dll", "ok.dll"])
    arc.write_bytes(b"not really an archive")
    game = tmp_path / "Game"
    with pytest.raises(ErmError) as exc:
        install.extract_archive(arc, game, "")
    assert "unsafe path" in str(exc.value)
    assert not (tmp_path / "escape.dll").exists()
    assert not game.exists() or not any(game.iterdir())


def test_extract_archive_reports_a_missing_extractor(tmp_path, monkeypatch):
    arc = tmp_path / "mod.rar"
    arc.write_bytes(b"Rar!\x1a\x07\x00not-a-zip")
    monkeypatch.setattr(install.shutil, "which", lambda n: None)
    # BadZipFile rather than ErmError so apply skips this one mod and keeps
    # going — aborting would discard the install record for everything that
    # already succeeded this run.
    with pytest.raises(zipfile.BadZipFile) as exc:
        install.extract_archive(arc, tmp_path / "Game", "")
    # Must name the archive and what's missing, not just "not a zip file".
    assert "mod.rar" in str(exc.value) and "bsdtar" in str(exc.value)


def test_extract_archive_strips_a_single_wrapper_directory(tmp_path):
    # Nexus DLL mods are often zipped inside one folder named after the mod and
    # its version. Elden Mod Loader only scans mods/*.dll, so extracting the
    # wrapper as-is nests the dll one level too deep and nothing loads.
    arc = tmp_path / "m.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("Vanilla - MapForGoblins - v2.0.5/MapForGoblins.dll", b"MZ")
        z.writestr("Vanilla - MapForGoblins - v2.0.5/MapForGoblins.ini", b"[x]")
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (game / "mods" / "MapForGoblins.dll").read_bytes() == b"MZ"
    assert not (game / "mods" / "Vanilla - MapForGoblins - v2.0.5").exists()
    assert sorted(files) == ["mods/MapForGoblins.dll", "mods/MapForGoblins.ini"]


def test_extract_archive_strip_keeps_nested_dirs_under_the_wrapper(tmp_path):
    # The wrapper goes; structure inside it stays (erquestlog needs its
    # questlog_lang/ dir sitting beside the dll).
    arc = tmp_path / "m.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("questlog/erquestlog.dll", b"MZ")
        z.writestr("questlog/questlog_lang/english.lang", b"en")
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (game / "mods" / "erquestlog.dll").exists()
    assert (game / "mods" / "questlog_lang" / "english.lang").read_bytes() == b"en"
    assert sorted(files) == ["mods/erquestlog.dll", "mods/questlog_lang/english.lang"]


def test_extract_archive_strip_leaves_a_single_top_level_file_alone(tmp_path):
    # NoWeight.dll / FasterRespawn.dll ship bare at the archive root — there's
    # no wrapper to strip and the dll must not be mistaken for one.
    arc = tmp_path / "m.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("NoWeight.dll", b"MZ")
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (game / "mods" / "NoWeight.dll").exists()
    assert files == ["mods/NoWeight.dll"]


def test_extract_archive_strip_leaves_multiple_top_level_entries_alone(tmp_path):
    # erdyes/ertransmogrify ship dll + ini + LICENSE at the root: no wrapper.
    arc = tmp_path / "m.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("erdyes.dll", b"MZ")
        z.writestr("erdyes.ini", b"[x]")
        z.writestr("LICENSE.txt", b"lic")
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (game / "mods" / "erdyes.dll").exists()
    assert len(files) == 3


def test_extract_archive_does_not_strip_by_default(tmp_path):
    # install="game" archives commonly contain exactly one top-level `mods/`
    # dir — stripping there would drop their dlls into Game/ instead of
    # Game/mods/ and silently unload eight working mods.
    arc = tmp_path / "m.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("mods/SkipTheIntro.dll", b"MZ")
    game = tmp_path / "Game"
    files = install.extract_archive(arc, game, "")
    assert (game / "mods" / "SkipTheIntro.dll").exists()
    assert files == ["mods/SkipTheIntro.dll"]
