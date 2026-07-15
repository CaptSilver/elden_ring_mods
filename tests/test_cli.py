import pytest

from ermlib import cli
from ermlib.errors import PathError


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
