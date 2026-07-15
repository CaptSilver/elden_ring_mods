import shutil
import subprocess
from pathlib import Path

from .errors import ErmError, PathError


def is_hardened(game):
    """Hardened iff our backup of the real EAC launcher exists."""
    return (Path(game) / "start_protected_game.exe.erm-backup").exists()


def harden_swap(game):
    """FS-only (no privilege): back up the real EAC launcher (ONCE) and swap in
    a copy of eldenring.exe. Returns the spg path. Idempotent-safe."""
    game = Path(game)
    spg = game / "start_protected_game.exe"
    eldenring = game / "eldenring.exe"
    backup = game / "start_protected_game.exe.erm-backup"
    if not eldenring.exists():
        raise PathError(f"eldenring.exe not found in {game} — can't harden")
    # CRITICAL: only back up if no backup exists. If a backup already exists,
    # spg is ALREADY the eldenring copy — backing it up again would overwrite
    # the real EAC-launcher backup with the eldenring copy, destroying the
    # only copy of the real launcher. This is the exact bug that made
    # community exe-swap scripts unsafe.
    try:
        if not backup.exists():
            if not spg.exists():
                raise PathError(f"start_protected_game.exe not found in {game} — nothing to swap")
            shutil.copy2(spg, backup)
        # (re)create the swapped exe from eldenring.exe
        if spg.exists():
            spg.unlink()
        shutil.copy2(eldenring, spg)
    except OSError as exc:
        raise PathError(f"failed to swap start_protected_game.exe: {exc}") from exc
    return spg


def unharden_restore(game):
    """FS-only: restore the real EAC launcher from backup, remove the swap+backup."""
    game = Path(game)
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"
    if not backup.exists():
        raise PathError(f"not hardened (no backup at {backup}) — nothing to restore")
    try:
        if spg.exists():
            spg.unlink()
        shutil.move(str(backup), str(spg))
    except OSError as exc:
        raise PathError(f"failed to restore start_protected_game.exe: {exc}") from exc


def set_immutable(path, on):
    """Privileged: sudo chattr +i/-i. Interactive (inherits the terminal for the
    sudo password prompt) — never capture stdout/stderr. Raises ErmError on
    failure. Monkeypatched in tests; never calls real sudo there."""
    flag = "+i" if on else "-i"
    try:
        r = subprocess.run(["sudo", "chattr", flag, str(path)])
    except FileNotFoundError as exc:
        raise ErmError(f"sudo/chattr not available: {exc}") from exc
    if r.returncode != 0:
        raise ErmError(
            f"chattr {flag} failed (rc {r.returncode}) — filesystem may not "
            "support immutable, or sudo declined"
        )
