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
    return install.find_extractor() is not None


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
    subprocess.run([install.find_extractor(), "-a", "-cf", str(path), "-C", str(src)]
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


def _tar_gz(path, members):
    """A .tar.gz fixture. `members` maps arcname -> bytes."""
    import io, tarfile
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_tar_gz_extracts_without_an_external_extractor(tmp_path, monkeypatch):
    # .tar.gz is the shape me3 ships its Linux build in, and the stdlib reads it.
    # Routing it through bsdtar would make the loader's own install depend on a
    # tool that is routinely missing from a non-login PATH.
    monkeypatch.setattr(install.shutil, "which", lambda _n: None)
    arc = _tar_gz(tmp_path / "me3.tar.gz", {"./bin/me3": b"ELF", "./LICENSE": b"x"})
    dest = tmp_path / "out"
    files = install.extract_archive(arc, dest, "")
    assert (dest / "bin" / "me3").read_bytes() == b"ELF"
    assert any(f.endswith("bin/me3") for f in files)


def test_tar_gz_on_an_interpreter_without_the_extraction_filter(tmp_path):
    # tarfile's `data` filter (and the `filter` kwarg extractall takes) arrived
    # in 3.11.4 / 3.12; Debian 12 ships 3.11.2 as its system Python. Without it
    # extractall raises TypeError halfway through apply, which escapes the
    # per-mod handler and kills the run before installed.json is written — so
    # the mod is on disk with nothing recording it. Skip this one archive the
    # way a missing bsdtar is skipped, and let the rest of the run finish.
    import tarfile
    arc = _tar_gz(tmp_path / "me3.tar.gz", {"./bin/me3": b"ELF"})
    saved = tarfile.data_filter
    del tarfile.data_filter
    try:
        with pytest.raises(zipfile.BadZipFile) as exc:
            install.extract_archive(arc, tmp_path / "out", "")
    finally:
        tarfile.data_filter = saved
    assert "me3.tar.gz" in str(exc.value) and "3.11.4" in str(exc.value)
    assert not (tmp_path / "out" / "bin").exists()


def test_tar_gz_refuses_a_member_that_escapes_the_destination(tmp_path):
    arc = _tar_gz(tmp_path / "evil.tar.gz", {"../escaped": b"pwned"})
    with pytest.raises(ErmError, match="unsafe path"):
        install.extract_archive(arc, tmp_path / "out", "")
    assert not (tmp_path / "escaped").exists()


def _wrapper_zip(path):
    """The one wrapper shape with both a nested dir and a top-level file under
    it — erquestlog's layout, which drives every re-apply collision case."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("questlog/erquestlog.dll", b"MZ")
        z.writestr("questlog/questlog_lang/english.lang", b"en")
    return path


def test_extract_archive_strip_replaces_a_colliding_directory(tmp_path):
    # apply never uninstalls before extracting, so a second `erm apply` lands on
    # the first one's output. The wrapper's dirs have to replace what's there —
    # move refuses to write over an existing dir, and a leftover file from an
    # older release must not survive the upgrade.
    arc = _wrapper_zip(tmp_path / "m.zip")
    game = tmp_path / "Game"
    install.extract_archive(arc, game, "mods", strip_wrapper=True)
    stale = game / "mods" / "questlog_lang" / "deutsch.lang"
    stale.write_bytes(b"old")
    (game / "mods" / "erquestlog.dll").write_bytes(b"OLD")

    files = install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert not stale.exists()
    assert (game / "mods" / "erquestlog.dll").read_bytes() == b"MZ"
    assert (game / "mods" / "questlog_lang" / "english.lang").read_bytes() == b"en"
    assert sorted(files) == ["mods/erquestlog.dll", "mods/questlog_lang/english.lang"]


def test_extract_archive_strip_replaces_a_file_where_a_directory_now_goes(tmp_path):
    # A release that turned a plain file into a directory (a lang file becoming
    # a lang/ folder) leaves the old file sitting exactly where the new dir has
    # to land; renaming a directory onto it fails outright.
    arc = _wrapper_zip(tmp_path / "m.zip")
    game = tmp_path / "Game"
    install.extract_archive(arc, game, "mods", strip_wrapper=True)
    clash = game / "mods" / "questlog_lang"
    shutil.rmtree(clash)
    clash.write_bytes(b"old single-file release")

    install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (clash / "english.lang").read_bytes() == b"en"


def test_extract_archive_strip_unlinks_a_colliding_symlink_without_following_it(tmp_path):
    # If the destination is a symlink to a directory, the tree it points at is
    # outside the game dir and is not ours to delete — drop the link, keep the
    # target.
    arc = _wrapper_zip(tmp_path / "m.zip")
    game = tmp_path / "Game"
    install.extract_archive(arc, game, "mods", strip_wrapper=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keepme.txt").write_bytes(b"not ours")
    link = game / "mods" / "questlog_lang"
    shutil.rmtree(link)
    link.symlink_to(outside, target_is_directory=True)

    install.extract_archive(arc, game, "mods", strip_wrapper=True)
    assert (outside / "keepme.txt").read_bytes() == b"not ours"
    assert not link.is_symlink()
    assert (link / "english.lang").read_bytes() == b"en"


def test_apply_ersc_removes_the_launcher_from_older_releases(tmp_path, tmp_game):
    # ERSC renamed its launcher to ersc_launcher.exe; the old one still works
    # enough to start the game unmodded-looking, so leaving it behind invites
    # launching the wrong binary after an upgrade.
    legacy = tmp_game / "launch_elden_ring_seamlesscoop.exe"
    legacy.write_bytes(b"MZ old")
    z = tmp_path / "ersc.zip"
    _make_ersc_zip(z)
    apply_ersc(z, tmp_game, password="hunter2")
    assert not legacy.exists()
    assert (tmp_game / "ersc_launcher.exe").exists()


def test_extract_archive_closes_the_zip_when_it_rejects_a_member(tmp_path, monkeypatch):
    # The guard runs between opening the archive and extracting it, so the
    # rejection path is the one that can leak the open ZipFile.
    opened = []
    real = zipfile.ZipFile

    class Recording(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            opened.append(self)

    monkeypatch.setattr(install.zipfile, "ZipFile", Recording)
    arc = tmp_path / "evil.zip"
    with real(arc, "w") as z:
        z.writestr("../evil.txt", b"pwned")
    with pytest.raises(ErmError):
        install.extract_archive(arc, tmp_path / "Game", "")
    assert opened, "the zip branch never ran"
    assert all(z.fp is None for z in opened), "ZipFile left open after the rejection"


def test_inject_password_refuses_a_settings_layout_it_cannot_write_into(tmp_path):
    """An ERSC update that renames the password key would otherwise be written
    back untouched and reported as success, starting a session with a blank
    password that anyone can join. The module exists to stop exactly that."""
    ini = tmp_path / "ersc_settings.ini"
    original = "[PASSWORD]\nsession_password = \n[SAVE]\nsave_file_extension = co2\n"
    ini.write_text(original)

    with pytest.raises(ErmError) as exc:
        inject_password(ini, "hunter2")

    assert "ersc_settings.ini" in str(exc.value)
    assert "cooppassword" in str(exc.value), "say which key was looked for"
    assert ini.read_text() == original, "must not half-write the file it refused"


def test_inject_password_accepts_an_unrecognised_layout_when_there_is_no_password(tmp_path):
    """No COOP_PASSWORD is a warned-about state, not an error -- there is
    nothing to inject, so an unfamiliar layout is not a failure here."""
    ini = tmp_path / "ersc_settings.ini"
    original = "[PASSWORD]\nsession_password = \n"
    ini.write_text(original)

    inject_password(ini, "")

    assert ini.read_text() == original


def _fake_bsdtar(directory):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / install.EXTRACTOR
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_the_extractor_is_found_in_the_homebrew_prefix_when_it_is_not_on_path(
        tmp_path, monkeypatch):
    """brew puts bsdtar in /home/linuxbrew/.linuxbrew/bin, and a non-login shell
    routinely has none of brew's bin on PATH. erm then called it "not installed"
    and skipped every .rar/.7z mod -- telling you to install something already
    sitting on the disk, and aborting the regulation merge as a knock-on."""
    brew = tmp_path / "linuxbrew" / "bin"
    exe = _fake_bsdtar(brew)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "EXTRACTOR_DIRS", (brew,))

    assert install.find_extractor() == str(exe)


def test_an_extractor_on_path_wins_over_the_homebrew_prefix(tmp_path, monkeypatch):
    """PATH is what the user chose; the fallback is only for when they didn't."""
    brew = tmp_path / "linuxbrew" / "bin"
    _fake_bsdtar(brew)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/bsdtar")
    monkeypatch.setattr(install, "EXTRACTOR_DIRS", (brew,))

    assert install.find_extractor() == "/usr/bin/bsdtar"


def test_a_non_executable_file_in_the_prefix_is_not_taken_as_the_extractor(
        tmp_path, monkeypatch):
    """A leftover name that can't be run is not an extractor -- handing it to
    subprocess would fail later and further from the cause."""
    brew = tmp_path / "linuxbrew" / "bin"
    brew.mkdir(parents=True)
    (brew / install.EXTRACTOR).write_text("not executable")
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "EXTRACTOR_DIRS", (brew,))

    assert install.find_extractor() is None
