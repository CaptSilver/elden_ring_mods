"""Install me3 content packages: normalize a downloaded mod archive to the me3
package layout (a folder mirroring the game's DVDBND hierarchy) and place it
under tools/me3/mods/<id>/."""
import shutil
from pathlib import Path

from . import install
from .errors import PathError
from .paths import is_safe_relpath

# ELDEN RING DVDBND top-level directories, lowercased. A folder that directly
# contains one of these (or regulation.bin) is a package root.
ASSET_DIRS = {
    "parts", "chr", "obj", "asset", "menu", "msg", "sfx", "sound", "event",
    "map", "action", "param", "font", "cutscene", "movie", "script", "material",
    "mtd", "remo", "shader", "other", "expression", "facegen",
}
# Sibling files that don't disqualify a folder from being a single wrapper.
# Includes ModEngine2 launcher companions (modengine2_launcher.exe,
# config_eldenring.toml, a launch .bat) that ship beside the mod/ folder in a
# full ME2-packaged archive — they sit at the staging root and are discarded,
# not part of the package, but shouldn't block descent into mod/.
DOC_EXTS = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".html", ".url", ".ini",
            ".exe", ".toml", ".bat"}


def find_package_root(staging):
    """Return the directory inside `staging` whose contents match the DVDBND
    hierarchy, descending through a single wrapper folder if needed, or None."""
    cur = Path(staging)
    while True:
        children = list(cur.iterdir())
        dirs = [c for c in children if c.is_dir()]
        if {d.name.lower() for d in dirs} & ASSET_DIRS or any(c.name.lower() == "regulation.bin" for c in children if c.is_file()):
            return cur
        stray = [c for c in children if c.is_file() and c.suffix.lower() not in DOC_EXTS]
        if len(dirs) == 1 and not stray:
            cur = dirs[0]
            continue
        return None


def list_option_dirs(base):
    """Immediate subdirectories of `base` that each look like a self-contained
    option (find_package_root succeeds inside them). Used to tell the user which
    `subdir` values are valid when an archive ships multiple variant folders."""
    out = []
    for d in sorted(p for p in Path(base).iterdir() if p.is_dir()):
        if find_package_root(d) is not None:
            out.append(d.name)
    return out


def _normalized(name):
    return "".join(c for c in name.lower() if c.isalnum())


def find_native_dll(base, mod_id):
    """The mod's own DLL under `base`, or None if it can't be picked confidently.

    Elden Mod Loader only scans mods/*.dll, so a mod shipping its DLL inside a
    folder has to be chainloaded by me3 instead — which means naming the exact
    file. Prefer a DLL whose name matches the mod id, since archives that carry
    a redistributable beside the real one would otherwise load the dependency
    and leave the mod dormant. Returning None (rather than guessing) makes the
    caller ask for an explicit choice.
    """
    dlls = sorted(Path(base).rglob("*.dll"))
    if not dlls:
        return None
    named = [d for d in dlls if _normalized(d.stem) == _normalized(mod_id)]
    if len(named) == 1:
        return named[0]
    if len(dlls) == 1:
        return dlls[0]
    return None


def install_me3_native(archive_path, mod_id, me3_dir, dll=None):
    """Extract `archive_path` to <me3_dir>/natives/<mod_id>/ and return the path
    to the DLL me3 should chainload. Raises PathError if it can't be identified.

    The whole archive is kept, not just the DLL: these mods read an .ini and
    sometimes a lang/ dir from beside the binary, so flattening would break them.
    """
    me3_dir = Path(me3_dir)
    dest = me3_dir / "natives" / mod_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # extract_archive enforces the zip-slip guard; game_dir=dest, no subdir.
    install.extract_archive(Path(archive_path), dest, "")
    if dll is not None:
        if not is_safe_relpath(dll):
            shutil.rmtree(dest, ignore_errors=True)
            raise PathError(f"{mod_id}: unsafe dll path {dll!r}")
        chosen = dest / dll
        if not chosen.is_file():
            shutil.rmtree(dest, ignore_errors=True)
            raise PathError(
                f"{mod_id}: dll {dll!r} not found in {Path(archive_path).name} "
                f"— check the profile's `dll` against the archive's actual layout")
        return str(chosen)
    chosen = find_native_dll(dest, mod_id)
    if chosen is None:
        found = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.dll"))
        shutil.rmtree(dest, ignore_errors=True)
        if found:
            raise PathError(
                f"{mod_id}: several DLLs in {Path(archive_path).name} — set `dll` in "
                f"the profile to one of: " + ", ".join(repr(f) for f in found))
        raise PathError(
            f"{mod_id}: no .dll found in {Path(archive_path).name} — this doesn't look "
            f"like a native mod; check the profile's install kind")
    return str(chosen)


def install_me3_package(archive_path, mod_id, me3_dir, subdir=None):
    """Extract `archive_path`, find its DVDBND root, and move it to
    <me3_dir>/mods/<mod_id>/. Returns (package_path_str, has_regulation).
    Raises PathError if no asset root can be located.

    Some Nexus archives ship several complete variant folders at the root
    (e.g. Minimal HUD's "OPTION 1 - Normal Backgrounds" / "OPTION 2 -
    Translucent Backgrounds") — find_package_root correctly refuses to guess
    between them. `subdir`, if given, names the one folder to search under,
    so the choice is explicit and reproducible instead of auto-guessed.
    """
    me3_dir = Path(me3_dir)
    staging = me3_dir / ".staging" / mod_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # extract_archive enforces the zip-slip guard; game_dir=staging, no subdir.
    install.extract_archive(Path(archive_path), staging, "")
    base = staging
    if subdir is not None:
        if not is_safe_relpath(subdir):
            shutil.rmtree(staging, ignore_errors=True)
            raise PathError(f"{mod_id}: unsafe subdir {subdir!r}")
        base = staging / subdir
        if not base.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            raise PathError(
                f"{mod_id}: subdir {subdir!r} not found in {Path(archive_path).name} "
                f"— check the profile's `subdir` against the archive's actual folder names")
    root = find_package_root(base)
    if root is None:
        options = list_option_dirs(base)
        shutil.rmtree(staging, ignore_errors=True)
        if options:
            raise PathError(
                f"{mod_id}: couldn't auto-place this archive — set `subdir` in the "
                f"profile to one of: " + ", ".join(repr(o) for o in options))
        raise PathError(
            f"{mod_id}: couldn't locate the game asset tree (parts/menu/msg/...) in "
            f"{Path(archive_path).name} — install it into a me3 package by hand")
    dest = me3_dir / "mods" / mod_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(dest))
    shutil.rmtree(staging, ignore_errors=True)
    has_regulation = (dest / "regulation.bin").exists()
    return str(dest), has_regulation
