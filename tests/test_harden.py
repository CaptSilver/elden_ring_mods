import pytest

from ermlib import cli, harden, paths
from ermlib.errors import ErmError, PathError

EAC_BYTES = b"EAC-LAUNCHER"
GAME_BYTES = b"GAME"


@pytest.fixture
def game(tmp_game):
    # tmp_game seeds both files with placeholder bytes; give them distinct
    # content so a test can tell "real EAC launcher" and "eldenring copy"
    # apart just by reading bytes back.
    (tmp_game / "start_protected_game.exe").write_bytes(EAC_BYTES)
    (tmp_game / "eldenring.exe").write_bytes(GAME_BYTES)
    return tmp_game


def test_harden_swap_replaces_spg_with_eldenring_copy_and_backs_up_original(game):
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)

    assert spg.read_bytes() == GAME_BYTES
    assert backup.read_bytes() == EAC_BYTES


def test_harden_swap_twice_does_not_clobber_the_real_backup(game):
    # THE critical invariant: on the second call, start_protected_game.exe is
    # already the eldenring copy. If harden_swap backed it up again, the
    # backup would end up holding GAME_BYTES instead of the real EAC launcher
    # -- destroying the only copy of the real launcher. This is the exact bug
    # that made community exe-swap scripts unsafe.
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)
    harden.harden_swap(game)

    assert backup.read_bytes() == EAC_BYTES


def test_harden_swap_raises_patherror_when_eldenring_missing(game):
    (game / "eldenring.exe").unlink()

    with pytest.raises(PathError):
        harden.harden_swap(game)


def test_unharden_restore_puts_back_original_bytes_and_removes_backup(game):
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)
    harden.unharden_restore(game)

    assert spg.read_bytes() == EAC_BYTES
    assert not backup.exists()


def test_unharden_restore_raises_patherror_when_not_hardened(game):
    with pytest.raises(PathError):
        harden.unharden_restore(game)


def test_is_hardened_true_after_swap_false_after_restore(game):
    assert harden.is_hardened(game) is False
    harden.harden_swap(game)
    assert harden.is_hardened(game) is True
    harden.unharden_restore(game)
    assert harden.is_hardened(game) is False


def _args(json=False):
    return type("A", (), {"json": json})()


def test_cmd_harden_swaps_and_sets_immutable_true(game, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))
    monkeypatch.setattr(paths, "find_steam_root", lambda: game.parent)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)

    spg = game / "start_protected_game.exe"
    rc = cli.cmd_harden(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert spg.read_bytes() == GAME_BYTES
    assert calls == [(spg, True)]
    assert "unharden" in out.lower() and "update" in out.lower()


def test_cmd_unharden_removes_immutable_false_then_restores(game, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))
    monkeypatch.setattr(paths, "find_steam_root", lambda: game.parent)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)

    spg = game / "start_protected_game.exe"
    harden.harden_swap(game)
    calls.clear()

    rc = cli.cmd_unharden(_args())
    capsys.readouterr()

    assert rc == 0
    assert calls == [(spg, False)]
    assert spg.read_bytes() == EAC_BYTES
    assert not harden.is_hardened(game)


def test_cmd_unharden_when_not_hardened_does_not_call_set_immutable(game, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))
    monkeypatch.setattr(paths, "find_steam_root", lambda: game.parent)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)

    rc = cli.cmd_unharden(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == []
    assert "not hardened" in out.lower()


def test_cmd_harden_surfaces_set_immutable_failure_cleanly(game, monkeypatch):
    def _boom(path, on):
        raise ErmError("chattr +i failed (rc 1)")

    monkeypatch.setattr(harden, "set_immutable", _boom)
    monkeypatch.setattr(paths, "find_steam_root", lambda: game.parent)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game)

    with pytest.raises(ErmError):
        cli.cmd_harden(_args())


def test_set_immutable_raises_ermerror_on_missing_sudo(monkeypatch, tmp_path):
    import subprocess

    def _raise(*a, **k):
        raise FileNotFoundError("sudo not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(ErmError):
        harden.set_immutable(tmp_path / "x", True)


def test_set_immutable_raises_ermerror_on_nonzero_rc(monkeypatch, tmp_path):
    import subprocess

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ErmError):
        harden.set_immutable(tmp_path / "x", True)
