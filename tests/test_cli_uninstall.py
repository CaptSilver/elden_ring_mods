import zipfile
from pathlib import Path

import pytest

from ermlib import cli, paths
from ermlib.errors import PathError


def _make_ersc_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def _seed_lock(tmp_path, version="v1.9.8", asset="seamless-coop-v1.9.8.zip"):
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        f'version = "{version}"\n'
        f'asset = "{asset}"\n'
        'sha256 = "a"\n'
        'source = "github"\n'
    )
    return asset


def _seed_profile(tmp_path, name="seamless-only"):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test profile"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
        'install = "game"\n'
    )


def _args(mod="seamless-coop", json=False):
    # "profile" defaults to seamless-only so this doubles as cmd_apply's args
    # in the tests below that install first, then uninstall what they installed.
    return type("A", (), {"mod": mod, "json": json, "profile": "seamless-only"})()


def test_uninstall_via_manifest_removes_recorded_files_prunes_dir_and_spares_stock(
        tmp_path, monkeypatch, capsys, tmp_game):
    # tmp_game already seeds eldenring.exe + start_protected_game.exe (stock files).
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    _seed_profile(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _make_ersc_zip(vendor / asset)

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
    _make_ersc_zip(vendor / asset)

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


def test_uninstall_never_removes_stock_files(tmp_path, monkeypatch, capsys, tmp_game):
    # Belt-and-suspenders on top of the manifest test: seed Game/ explicitly
    # with stock files + ersc files, uninstall, and check the stock files by
    # name one more time in isolation.
    game_dir = tmp_game
    asset = _seed_lock(tmp_path)
    _seed_profile(tmp_path)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _make_ersc_zip(vendor / asset)

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
