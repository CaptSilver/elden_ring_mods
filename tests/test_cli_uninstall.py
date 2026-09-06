import json
import zipfile
from pathlib import Path

import pytest

from ermlib import cli, paths
from ermlib.errors import PathError
from tests.ersc_fixtures import make_ersc_zip, seed_profile


def _seed_lock(tmp_path, version="v1.9.8", asset="seamless-coop-v1.9.8.zip"):
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        f'version = "{version}"\n'
        f'asset = "{asset}"\n'
        'sha256 = "a"\n'
        'source = "github"\n'
    )
    return asset


def _args(mod="seamless-coop", json=False):
    # "profile" defaults to seamless-only so this doubles as cmd_apply's args
    # in the tests below that install first, then uninstall what they installed.
    return type("A", (), {"mod": mod, "json": json, "profile": "seamless-only"})()


def test_uninstall_via_manifest_removes_recorded_files_prunes_dir_and_spares_stock(
        tmp_path, monkeypatch, capsys, tmp_game):
    # tmp_game already seeds eldenring.exe + start_protected_game.exe (stock files).
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    seed_profile(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    make_ersc_zip(vendor / asset)

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    # install first, via the same path `erm apply` uses, so installed.json
    # gets populated the real way.
    rc = cli.cmd_apply(_args())
    assert rc == 0
    assert (tmp_path / "installed.json").exists()
    import json
    recorded = json.loads((tmp_path / "installed.json").read_text())
    assert "seamless-coop" in recorded
    assert "ersc_launcher.exe" in recorded["seamless-coop"]["files"]

    capsys.readouterr()  # drain apply's output

    rc = cli.cmd_uninstall(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "ersc_launcher.exe").exists()
    assert not (game_dir / "SeamlessCoop").exists()          # emptied dir pruned
    assert (game_dir / "eldenring.exe").exists()              # stock file survives
    assert (game_dir / "start_protected_game.exe").exists()   # stock file survives

    after = json.loads((tmp_path / "installed.json").read_text())
    assert "seamless-coop" not in after
    assert "doctor" in out.lower()


def test_uninstall_falls_back_to_vendor_archive_when_no_manifest_entry(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    make_ersc_zip(vendor / asset)

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    # simulate files that got installed before erm tracked state: extract by
    # hand, but never write installed.json.
    with zipfile.ZipFile(vendor / asset) as z:
        z.extractall(game_dir)
    assert not (tmp_path / "installed.json").exists()

    rc = cli.cmd_uninstall(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "ersc_launcher.exe").exists()
    assert not (game_dir / "SeamlessCoop").exists()
    assert (game_dir / "eldenring.exe").exists()
    assert (game_dir / "start_protected_game.exe").exists()
    assert "vendor archive" in out.lower()


def test_uninstall_nothing_recorded_and_no_vendor_archive_raises_patherror(
        tmp_path, monkeypatch, tmp_game):
    game_dir = tmp_game
    _seed_lock(tmp_path)
    (tmp_path / "vendor").mkdir()   # empty — asset never fetched

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PathError):
        cli.cmd_uninstall(_args())


def test_uninstall_refuses_paths_outside_game(tmp_path, monkeypatch, capsys, tmp_game):
    # A hand-edited (or trojaned-fallback-derived) installed.json whose files
    # list contains a traversal path must not delete anything outside Game/.
    game_dir = tmp_game
    _seed_lock(tmp_path)

    victim = tmp_path / "victim.txt"        # lives OUTSIDE the game dir
    victim.write_text("precious")

    import json
    (tmp_path / "installed.json").write_text(json.dumps({
        "seamless-coop": {
            "version": "v1.9.8",
            "archive": "x.zip",
            "files": ["../victim.txt", "ersc_launcher.exe"],
        }
    }))
    # a real ersc file inside the game dir, so the safe entry still gets removed
    (game_dir / "ersc_launcher.exe").write_bytes(b"\x00")

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_uninstall(_args())
    out = capsys.readouterr().out

    assert rc == 0                                  # clean exit, no crash
    assert victim.exists()                          # outside file untouched
    assert victim.read_text() == "precious"
    assert not (game_dir / "ersc_launcher.exe").exists()   # safe entry removed
    assert "refus" in out.lower()                   # warned about the unsafe path


def test_uninstall_symlink_to_ingame_file_removes_only_the_symlink(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A recorded path that's a symlink to a stock in-game file must delete the
    # symlink itself, never follow it to eldenring.exe. Containment passes
    # (target is inside Game/), so only unlinking the LITERAL path keeps the
    # "stock files survive" guarantee.
    game_dir = tmp_game            # already has eldenring.exe (stock)
    link = game_dir / "ersc_launcher.exe"
    link.symlink_to("eldenring.exe")   # relative target, resolves inside Game/

    import json
    (tmp_path / "installed.json").write_text(json.dumps({
        "seamless-coop": {
            "version": "v1.9.8",
            "archive": "x.zip",
            "files": ["ersc_launcher.exe"],
        }
    }))

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_uninstall(_args())
    assert rc == 0
    assert (game_dir / "eldenring.exe").exists()     # symlink target untouched
    assert not link.exists() and not link.is_symlink()   # the symlink is gone


def test_uninstall_corrupt_vendor_archive_raises_patherror(
        tmp_path, monkeypatch, tmp_game):
    # Fallback path with a garbage (non-zip) vendor file must raise a clean
    # PathError, not a raw zipfile.BadZipFile.
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / asset).write_bytes(b"this is not a zip file")   # corrupted

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PathError):
        cli.cmd_uninstall(_args())


def _write_profile(profiles_dir, name, mods_toml):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test fixture profile"\n'
        '\n' + mods_toml
    )


def test_uninstall_profile_removes_me3_package_and_regenerates_profile(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A me3-package mod has no Game/ file list (it lives in tools/me3/mods/),
    # so uninstalling the profile it belongs to must remove that package dir
    # (not fall into the vendor-archive/files fallback), drop the state
    # entry, and the regenerated profile must no longer list it.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "unit-cosmetic",
        '[[mods]]\n'
        'id = "unit-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 999\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
    )
    (tmp_path / "mods.lock.toml").write_text(
        '[unit-mod]\n'
        'version = "1.0"\n'
        'asset = "unit-mod.zip"\n'
        'sha256 = "a"\n'
        'source = "nexus"\n'
    )
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "unit-mod.zip", "w") as z:
        z.writestr("parts/wp_a.dcx", b"x")

    apply_args = type("A", (), {"profile": "unit-cosmetic", "json": False})()
    rc = cli.cmd_apply(apply_args)
    capsys.readouterr()
    assert rc == 0

    pkg_dir = tmp_path / "tools" / "me3" / "mods" / "unit-mod"
    prof = tmp_path / "tools" / "me3" / "erm-coop.me3"
    assert pkg_dir.exists()
    assert "unit-mod" in prof.read_text()

    rc = cli.cmd_uninstall(type("A", (), {"mod": "unit-cosmetic", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert not pkg_dir.exists()

    import json
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "unit-mod" not in state
    assert "unit-mod" not in prof.read_text()
    assert "removed me3 package" in out.lower()


def test_uninstall_profile_me3_rmtree_failure_does_not_abort_other_removals(
        tmp_path, monkeypatch, capsys, tmp_game):
    # If shutil.rmtree on a me3-package dir blows up (permission denied, a
    # file still open, whatever), that must not sink the rest of a
    # profile-uninstall — it should warn, still forget the mod, and let the
    # OTHER mod in the same run (a plain Game/ file removal) still happen and
    # installed.json still get written. Put the failing mod FIRST in the
    # profile so the old unguarded code — which lets the OSError propagate
    # straight out of cmd_uninstall — would abort before ever touching the
    # second mod or calling write_state.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "mixed",
        '[[mods]]\n'
        'id = "unit-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 999\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
        'install = "game"\n'
    )
    asset = _seed_lock(tmp_path)
    (tmp_path / "mods.lock.toml").write_text(
        (tmp_path / "mods.lock.toml").read_text() +
        '\n[unit-mod]\n'
        'version = "1.0"\n'
        'asset = "unit-mod.zip"\n'
        'sha256 = "a"\n'
        'source = "nexus"\n'
    )
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "unit-mod.zip", "w") as z:
        z.writestr("parts/wp_a.dcx", b"x")
    make_ersc_zip(vendor / asset)

    apply_args = type("A", (), {"profile": "mixed", "json": False})()
    rc = cli.cmd_apply(apply_args)
    capsys.readouterr()
    assert rc == 0

    pkg_dir = tmp_path / "tools" / "me3" / "mods" / "unit-mod"
    assert pkg_dir.exists()
    assert (game_dir / "ersc_launcher.exe").exists()

    def _raise_rmtree(path, *a, **kw):
        raise OSError("permission denied (simulated)")
    monkeypatch.setattr(cli.shutil, "rmtree", _raise_rmtree)

    rc = cli.cmd_uninstall(type("A", (), {"mod": "mixed", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert pkg_dir.exists()                     # rmtree failed — left on disk
    assert "could not remove" in out.lower()     # warned, not silent
    # the OTHER mod in the same profile-uninstall run still got removed
    assert not (game_dir / "ersc_launcher.exe").exists()
    assert not (game_dir / "SeamlessCoop").exists()

    import json
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "unit-mod" not in state       # forgotten despite the rmtree failure
    assert "seamless-coop" not in state  # normal removal unaffected


def test_uninstall_me3_package_outside_mods_dir_is_refused(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A hand-edited (or corrupted) installed.json entry can record ANY string
    # as "package" — e.g. "/etc" or a "../../x" traversal. The me3-package
    # removal branch used to rmtree it unchecked, unlike the Game/-file path
    # in this same function which re-validates every path. Require the
    # recorded package to resolve under ME3_DIR/"mods" before touching it;
    # anything outside gets refused (warned + the stale record dropped), not
    # deleted.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    sentinel = tmp_path / "sentinel-outside"
    sentinel.mkdir()
    (sentinel / "precious.txt").write_text("do not delete me")

    import json
    (tmp_path / "installed.json").write_text(json.dumps({
        "evil-mod": {
            "version": "1.0",
            "archive": "evil.zip",
            "kind": "me3-package",
            "package": str(sentinel),
        }
    }))

    rc = cli.cmd_uninstall(type("A", (), {"mod": "evil-mod", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert sentinel.exists()                       # not deleted
    assert (sentinel / "precious.txt").exists()
    assert "refus" in out.lower()                   # warned about the unsafe path

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "evil-mod" not in state                  # stale record still dropped


def test_uninstall_never_removes_stock_files(tmp_path, monkeypatch, capsys, tmp_game):
    # Belt-and-suspenders on top of the manifest test: seed Game/ explicitly
    # with stock files + ersc files, uninstall, and check the stock files by
    # name one more time in isolation.
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    seed_profile(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    make_ersc_zip(vendor / asset)

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    cli.cmd_apply(_args())
    capsys.readouterr()

    rc = cli.cmd_uninstall(_args())
    capsys.readouterr()

    assert rc == 0
    assert (game_dir / "eldenring.exe").exists()
    assert (game_dir / "start_protected_game.exe").exists()


def test_uninstall_removes_a_me3_native_dir(tmp_path, monkeypatch, capsys, tmp_game):
    # A native lives under tools/me3/natives/<id>/, not Game/ — the file-list
    # path would find nothing and leave the whole dir orphaned.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    nat = tmp_path / "tools" / "me3" / "natives" / "questpath" / "QuestPath"
    nat.mkdir(parents=True)
    (nat / "QuestPath.dll").write_bytes(b"MZ")
    (tmp_path / "installed.json").write_text(json.dumps({
        "questpath": {"version": "1.3.0", "archive": "questpath.zip",
                      "kind": "me3-native",
                      "native": str(nat / "QuestPath.dll")}}))

    rc = cli.cmd_uninstall(type("A", (), {"mod": "questpath", "json": False})())
    assert rc == 0
    assert not (tmp_path / "tools" / "me3" / "natives" / "questpath").exists()
    assert json.loads((tmp_path / "installed.json").read_text()) == {}


def test_uninstall_refuses_a_native_recorded_outside_the_natives_dir(
        tmp_path, monkeypatch, capsys, tmp_game):
    # installed.json is hand-editable; a path escaping tools/me3/natives/ must
    # not be rmtree'd. Same containment reasoning as the me3-package branch.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    (tmp_path / "installed.json").write_text(json.dumps({
        "evil": {"version": "1", "archive": "e.zip", "kind": "me3-native",
                 "native": str(outside / "evil.dll")}}))

    rc = cli.cmd_uninstall(type("A", (), {"mod": "evil", "json": False})())
    out = capsys.readouterr().out
    assert rc == 0
    assert (outside / "keep.txt").exists()
    assert "refusing" in out.lower()


def test_uninstall_drops_the_merged_package(tmp_path, monkeypatch, capsys, tmp_game):
    """Merging strips the shared path out of its source packages, so derived
    output that outlives an uninstall becomes the sole provider of content
    nothing ships anymore. Uninstalling must drop it; the next apply rebuilds it."""
    from ermlib import conflicts

    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "unit-merged",
        '[[mods]]\n'
        'id = "unit-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 999\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
    )
    (tmp_path / "mods.lock.toml").write_text(
        '[unit-mod]\n'
        'version = "1.0"\n'
        'asset = "unit-mod.zip"\n'
        'sha256 = "a"\n'
        'source = "nexus"\n'
    )
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "unit-mod.zip", "w") as z:
        z.writestr("parts/wp_a.dcx", b"x")

    assert cli.cmd_apply(type("A", (), {"profile": "unit-merged", "json": False})()) == 0
    capsys.readouterr()

    # Stand in for a merge having run on a previous apply.
    merged = tmp_path / "tools" / "me3" / "mods" / conflicts.MERGED_ID / "msg" / "engus"
    merged.mkdir(parents=True)
    (merged / "menu_dlc02.msgbnd.dcx").write_bytes(b"stale merged output")

    rc = cli.cmd_uninstall(type("A", (), {"mod": "unit-merged", "json": False})())
    capsys.readouterr()

    assert rc == 0
    assert not merged.parent.parent.exists()
    assert conflicts.MERGED_ID not in json.loads(
        (tmp_path / "installed.json").read_text())


def test_uninstall_forgets_the_merged_state_entry_even_without_a_stale_dir(
        tmp_path, monkeypatch, capsys, tmp_game):
    """clear_merged() only ever touches the filesystem -- installed.json has
    to drop the _merged entry too, or erm would keep reporting/reconciling it
    as installed even with nothing on disk to back it."""
    from ermlib import conflicts, state as state_mod

    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    state = {"sample": {"version": "1.0", "archive": "sample.zip",
                         "kind": "me3-package", "package": "tools/me3/mods/sample"}}
    state_mod.record_merged(state, f"tools/me3/mods/{conflicts.MERGED_ID}")
    (tmp_path / "installed.json").write_text(json.dumps(state))
    (tmp_path / "tools" / "me3" / "mods" / "sample").mkdir(parents=True)

    rc = cli.cmd_uninstall(type("A", (), {"mod": "sample", "json": False})())
    capsys.readouterr()

    assert rc == 0
    assert conflicts.MERGED_ID not in json.loads(
        (tmp_path / "installed.json").read_text())


def test_uninstall_json_is_one_document_with_the_doctor_nested(
        tmp_path, monkeypatch, capsys, tmp_game):
    # uninstall printed its report, the literal line "Safety check (erm doctor):",
    # then a SECOND document — `erm --json uninstall` could not be parsed at all.
    game_dir = tmp_game
    (game_dir / "ersc_launcher.exe").write_bytes(b"\x00")
    (tmp_path / "installed.json").write_text(json.dumps({
        "seamless-coop": {"version": "v1.9.8", "archive": "x.zip",
                          "files": ["ersc_launcher.exe"]}}))
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_uninstall(_args(json=True))
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    # The fake Game/ has no regulation.bin, so the nested doctor cannot identify
    # a build and warns -- which is the proof that it ran the build checks at all.
    assert data["doctor"]["worst"] == "warn"
    assert any("game build" in i["message"] for i in data["doctor"]["items"]), (
        "the nested doctor must run the same checks erm doctor runs")
    assert any("seamless-coop" in i["message"] for i in data["items"])


def test_uninstall_exit_code_reports_a_mod_artifact_left_behind(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A proxy DLL still sitting next to the real EAC launcher is the mixed
    # state doctor fails on. uninstall printed that ✗ and returned 0 anyway, so
    # `erm uninstall <mod> && …` read a ban-risk install as clean.
    game_dir = tmp_game
    (game_dir / "ersc_launcher.exe").write_bytes(b"\x00")
    (game_dir / "dinput8.dll").write_bytes(b"\x00")
    (tmp_path / "installed.json").write_text(json.dumps({
        "seamless-coop": {"version": "v1.9.8", "archive": "x.zip",
                          "files": ["ersc_launcher.exe"]}}))
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_uninstall(_args())
    out = capsys.readouterr().out

    assert rc == 1
    assert "dinput8.dll" in out
    assert not (game_dir / "ersc_launcher.exe").exists()   # the removal still happened


def test_uninstall_forgets_a_me3_package_entry_with_no_recorded_path(
        tmp_path, monkeypatch, capsys, tmp_game):
    # installed.json is hand-editable, so an entry can carry kind but no
    # package. The other two recorded kinds warn and forget it; this branch
    # hard-keyed it and came out as a raw KeyError traceback.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "installed.json").write_text(json.dumps({
        "broken-mod": {"version": "1.0", "archive": "b.zip", "kind": "me3-package"}}))

    rc = cli.cmd_uninstall(type("A", (), {"mod": "broken-mod", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert "no recorded package path" in out
    assert json.loads((tmp_path / "installed.json").read_text()) == {}


def test_uninstall_forgets_an_empty_me3_package_path_rather_than_resolving_cwd(
        tmp_path, monkeypatch, capsys, tmp_game):
    # An empty string is Path("."), which resolves to the cwd — containment
    # would refuse it with a message naming a directory nobody recorded.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "installed.json").write_text(json.dumps({
        "broken-mod": {"version": "1.0", "archive": "b.zip",
                       "kind": "me3-package", "package": ""}}))

    rc = cli.cmd_uninstall(type("A", (), {"mod": "broken-mod", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert "no recorded package path" in out
    assert json.loads((tmp_path / "installed.json").read_text()) == {}


def test_uninstall_refuses_a_recorded_file_that_resolves_outside_the_game_dir(
        tmp_path, monkeypatch, capsys, tmp_game):
    # The recorded path is relpath-clean, so is_safe_relpath passes it — it
    # escapes by traversing a symlinked DIRECTORY inside Game/. Only the
    # resolve() containment check catches that one, and the literal it would
    # otherwise unlink is a real file outside the game dir, not a symlink.
    game_dir = tmp_game
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.dll").write_text("precious")
    (game_dir / "mods").mkdir()
    (game_dir / "mods" / "escape").symlink_to(outside, target_is_directory=True)
    (game_dir / "ersc_launcher.exe").write_bytes(b"\x00")

    (tmp_path / "installed.json").write_text(json.dumps({
        "seamless-coop": {"version": "v1.9.8", "archive": "x.zip",
                          "files": ["mods/escape/victim.dll", "ersc_launcher.exe"]}}))
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_uninstall(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert (outside / "victim.dll").read_text() == "precious"
    # Name the layer: both warnings start with "refusing", so a bare "refus"
    # can't tell this one from the is_safe_relpath gate above it.
    assert "outside game dir" in out
    # A refused entry warns without aborting the rest of the list.
    assert not (game_dir / "ersc_launcher.exe").exists()


def test_switch_json_is_one_document(tmp_path, monkeypatch, capsys, tmp_game):
    # switch emitted its own uninstall document, then delegated to apply for
    # two more — three JSON documents from one command.
    game_dir = tmp_game
    (game_dir / "ersc_launcher.exe").write_bytes(b"\x00")
    asset = _seed_lock(tmp_path)
    seed_profile(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    make_ersc_zip(vendor / asset)
    (tmp_path / "installed.json").write_text(json.dumps({
        "old-mod": {"version": "1", "archive": "x.zip", "files": ["ersc_launcher.exe"]}}))
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    rc = cli.cmd_switch(type("A", (), {"profile": "seamless-only", "json": True})())
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert any("switching to seamless-only" in i["message"] for i in data["items"])
    assert any("old-mod" in i["message"] for i in data["items"])
    # The fake Game/ has no regulation.bin, so the nested doctor cannot identify
    # a build and warns -- which is the proof that it ran the build checks at all.
    assert data["doctor"]["worst"] == "warn"
    assert any("game build" in i["message"] for i in data["doctor"]["items"]), (
        "the nested doctor must run the same checks erm doctor runs")
