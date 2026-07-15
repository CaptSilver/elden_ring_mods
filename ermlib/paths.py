import os
from pathlib import Path

from .errors import PathError

APPID = "1245620"

_STEAM_ROOTS = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
]


def same_location(a, b):
    a, b = Path(a), Path(b)
    try:
        return a.samefile(b)
    except (FileNotFoundError, OSError):
        return False


def find_steam_root():
    for cand in _STEAM_ROOTS:
        p = Path(cand).expanduser()
        if (p / "steamapps").is_dir():
            return p
    raise PathError("no Steam installation found (looked in ~/.steam, ~/.local/share/Steam)")


def _library_dirs(steam_root):
    """Every steamapps dir Steam knows about, from libraryfolders.vdf."""
    dirs = [steam_root / "steamapps"]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        import re
        for m in re.finditer(r'"path"\s*"([^"]+)"', vdf.read_text(errors="ignore")):
            dirs.append(Path(m.group(1)) / "steamapps")
    return dirs


def find_game_dir(steam_root):
    for sa in _library_dirs(steam_root):
        g = sa / "common" / "ELDEN RING" / "Game"
        if g.is_dir():
            return g
    raise PathError("ELDEN RING/Game not found in any Steam library")


def find_prefix(steam_root):
    for sa in _library_dirs(steam_root):
        pfx = sa / "compatdata" / APPID / "pfx"
        if pfx.is_dir():
            return pfx
    raise PathError(f"Proton prefix compatdata/{APPID}/pfx not found")


def find_save_dir(steam_root):
    pfx = find_prefix(steam_root)
    roots = [
        pfx / "drive_c/users/steamuser/AppData/Roaming/EldenRing",
        pfx / "drive_c/users/steamuser/Application Data/EldenRing",
    ]
    for root in roots:
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / "ER0000.sl2").exists():
                    return child
    raise PathError("no EldenRing save dir with ER0000.sl2 found in prefix")
