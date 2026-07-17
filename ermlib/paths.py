import re
from pathlib import Path, PurePosixPath

from .errors import PathError

APPID = "1245620"


def is_safe_relpath(name):
    """True if `name` is a plain relative path with no `..` escape.

    The zip-slip guard for install (archive members), uninstall
    (recorded/derived file lists), and me3-package subdir selection. Shared
    across the install/uninstall/me3-package call sites so the security check
    can't drift between them. Absolute paths and any `..` component are
    rejected.
    """
    pp = PurePosixPath(name)
    return not (pp.is_absolute() or ".." in pp.parts)

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
        try:
            text = vdf.read_text(errors="ignore")
        except OSError:
            # Unreadable libraryfolders.vdf (perms, TOCTOU race) — fall back to
            # the primary library rather than leaking OSError to the CLI.
            return dirs
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            dirs.append(Path(m.group(1)) / "steamapps")
    return dirs


def find_game_dir(steam_root):
    for sa in _library_dirs(steam_root):
        g = sa / "common" / "ELDEN RING" / "Game"
        if g.is_dir():
            return g
    raise PathError("ELDEN RING/Game not found in any Steam library")


def reshade_active(game_dir):
    """True if ReShade is installed in this game dir via reshade-steam-proton,
    which links Game/dxgi.dll -> .../ReShade{32,64}.dll.

    This is per-machine: a box that never ran the ReShade installer has no such
    link, so `erm launch-option` there stays ReShade-free. We only recognise the
    symlink we (well, the script) placed — a plain dxgi.dll or a DXVK link isn't
    ReShade, so we don't prepend an override that would do nothing.
    """
    dxgi = Path(game_dir) / "dxgi.dll"
    if not dxgi.is_symlink():
        return False
    try:
        target = dxgi.readlink()
    except OSError:
        return False
    return target.name.lower().startswith("reshade")


def find_prefix(steam_root):
    for sa in _library_dirs(steam_root):
        pfx = sa / "compatdata" / APPID / "pfx"
        if pfx.is_dir():
            return pfx
    raise PathError(f"Proton prefix compatdata/{APPID}/pfx not found")


def find_compatdata(steam_root):
    """The compatdata/<APPID> dir (parent of pfx) for STEAM_COMPAT_DATA_PATH."""
    for sa in _library_dirs(steam_root):
        cd = sa / "compatdata" / APPID
        if cd.is_dir():
            return cd
    raise PathError(f"compatdata/{APPID} not found")


_PROTON_ROOTS = [
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/steamapps/common",
    "~/.local/share/Steam/steamapps/common",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common",
]


def _proton_version_key(proton_path):
    # Natural sort on the install dir name (e.g. "GE-Proton10-31") so
    # "GE-Proton10-31" correctly outranks "GE-Proton9-20" — a plain string
    # sort puts "9" after "1" and picks the wrong "highest" version once a
    # major version hits double digits.
    return [int(t) if t.isdigit() else t
            for t in re.split(r"(\d+)", proton_path.parent.name)]


def find_proton():
    """Locate a Proton install to run Windows tools under.

    Prefers the newest GE-Proton (better compatibility for odd tools like
    mod generators) over a stock Proton build. Returns the path to the
    `proton` launcher script, or None if no Steam Play tool is installed.
    """
    candidates = []
    for root in _PROTON_ROOTS:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for d in root.iterdir():
            p = d / "proton"
            if p.exists():
                candidates.append(p)
    if not candidates:
        return None
    ge = [c for c in candidates if "GE-Proton" in c.parent.name]
    pool = ge or candidates
    return max(pool, key=_proton_version_key)


def find_save_dir(steam_root):
    pfx = find_prefix(steam_root)
    roots = [
        pfx / "drive_c/users/steamuser/AppData/Roaming/EldenRing",
        pfx / "drive_c/users/steamuser/Application Data/EldenRing",
    ]
    for root in roots:
        if root.is_dir():
            try:
                children = sorted(root.iterdir())
            except OSError:
                # Unreadable save root (perms, TOCTOU race) — skip it and keep
                # looking; end by raising PathError, never leaking OSError.
                continue
            for child in children:
                if child.is_dir() and (
                    (child / "ER0000.sl2").exists() or (child / "ER0000.co2").exists()
                ):
                    return child
    raise PathError("no EldenRing save dir with ER0000.sl2 or ER0000.co2 found in prefix")
