import pytest

from ermlib import cli
from ermlib.errors import ErmError, PathError


def test_launch_option_string(capsys):
    rc = cli.cmd_launch_option(type("A", (), {"json": False})())
    out = capsys.readouterr().out
    assert 'start_protected_game.exe/ersc_launcher.exe' in out
    assert out.count('"') >= 2          # quoting preserved
    assert rc == 0


def test_audit_on_fixture_save(capsys, tmp_path):
    from tests.conftest import REAL_SAVE
    if not REAL_SAVE.exists():
        pytest.skip("no fixture")
    args = type("A", (), {"json": False, "save": str(REAL_SAVE)})()
    rc = cli.cmd_audit(args)
    out = capsys.readouterr().out
    assert "cannot" in out.lower()      # the honesty caveat always prints
    assert rc == 0


def test_audit_bad_path_raises_patherror():
    args = type("A", (), {"json": False, "save": "/nonexistent/ER0000.sl2"})()
    with pytest.raises(PathError):
        cli.cmd_audit(args)


def test_restore_resolves_snapshot_name_under_backups(tmp_path, monkeypatch):
    # `erm restore <name>` takes a snapshot NAME from backups/, per the README —
    # not a cwd-relative path. Seed backups/snap.co2 and confirm restore pulls
    # from there rather than failing to find a "snap.co2" next to the cwd.
    from ermlib import paths

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "snap.co2").write_bytes(b"snapshot-data")

    save_dir = tmp_path / "save"
    save_dir.mkdir()

    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_save_dir", lambda root: save_dir)
    monkeypatch.chdir(tmp_path)

    args = type("A", (), {"backup": "snap.co2"})()
    rc = cli.cmd_restore(args)
    assert rc == 0
    assert (save_dir / "ER0000.co2").read_bytes() == b"snapshot-data"


def test_apply_missing_vendor_archive_raises_clean_error(tmp_path, monkeypatch):
    # Fresh clone that runs `apply` before `fetch`: the lockfile names an asset
    # that was never downloaded. Must be a clean ErmError, not a raw
    # FileNotFoundError from zipfile.ZipFile deep inside install.apply_ersc.
    from ermlib import paths

    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "vendor").mkdir()
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'asset = "seamless-coop-v1.9.8.zip"\n'
        'sha256 = "abc"\n'
        'source = "github"\n'
    )

    args = type("A", (), {})()
    with pytest.raises(ErmError):
        cli.cmd_apply(args)


def test_verify_missing_asset_key_warns_instead_of_crashing(tmp_path, monkeypatch, capsys):
    # A lock entry missing "asset" made Path("vendor")/"" resolve to the vendor
    # dir itself -> IsADirectoryError from sha256_file. Must warn and move on.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'source = "github"\n'
    )
    args = type("A", (), {"json": False})()
    rc = cli.cmd_verify(args)
    out = capsys.readouterr().out
    assert "no asset" in out.lower()
