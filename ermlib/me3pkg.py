"""Install me3 content packages: normalize a downloaded mod archive to the me3
package layout (a folder mirroring the game's DVDBND hierarchy) and place it
under tools/me3/mods/<id>/."""
import shutil
from pathlib import Path

from . import install
from .errors import PathError

# ELDEN RING DVDBND top-level directories, lowercased. A folder that directly
# contains one of these (or regulation.bin) is a package root.
ASSET_DIRS = {
    "parts", "chr", "obj", "asset", "menu", "msg", "sfx", "sound", "event",
    "map", "action", "param", "font", "cutscene", "movie", "script", "material",
    "mtd", "remo", "shader", "other", "expression", "facegen",
}
# Sibling files that don't disqualify a folder from being a single wrapper.
DOC_EXTS = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".html", ".url", ".ini"}


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


def install_me3_package(archive_path, mod_id, me3_dir):
    """Extract `archive_path`, find its DVDBND root, and move it to
    <me3_dir>/mods/<mod_id>/. Returns (package_path_str, has_regulation).
    Raises PathError if no asset root can be located."""
    me3_dir = Path(me3_dir)
    staging = me3_dir / ".staging" / mod_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # extract_archive enforces the zip-slip guard; game_dir=staging, no subdir.
    install.extract_archive(Path(archive_path), staging, "")
    root = find_package_root(staging)
    if root is None:
        shutil.rmtree(staging, ignore_errors=True)
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
