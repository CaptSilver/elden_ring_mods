from ermlib import cli


def test_launch_option_string(capsys):
    rc = cli.cmd_launch_option(type("A", (), {"json": False})())
    out = capsys.readouterr().out
    assert 'start_protected_game.exe/ersc_launcher.exe' in out
    assert out.count('"') >= 2          # quoting preserved
    assert rc == 0


def test_audit_on_fixture_save(capsys, tmp_path):
    from tests.conftest import REAL_SAVE
    if not REAL_SAVE.exists():
        import pytest; pytest.skip("no fixture")
    args = type("A", (), {"json": False, "save": str(REAL_SAVE)})()
    rc = cli.cmd_audit(args)
    out = capsys.readouterr().out
    assert "cannot" in out.lower()      # the honesty caveat always prints
    assert rc == 0
