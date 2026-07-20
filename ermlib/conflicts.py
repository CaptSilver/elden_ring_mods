"""Find and resolve files that two me3 packages both provide.

me3 maps one file per game-relative path and picks a winner silently -- it
doesn't even warn (me3 issue #44, open since 2025). So a collision means one
mod's content just isn't in the game, with no symptom unless you happen to
notice missing text. Detecting that is the point of this module; merging is
what we do about the one case we can resolve.
"""
import shutil
from pathlib import Path

from .errors import ErmError
from .merge import STRATEGIES
from .paths import is_safe_relpath

MERGED_ID = "_merged"


class ConflictError(ErmError):
    """Two mods provide the same file and nothing says how to resolve it."""


def _package_dir(me3_dir, mod_id):
    return Path(me3_dir) / "mods" / mod_id


def index_paths(me3_dir, mod_ids):
    """Map each game-relative path to the mods providing it, in `mod_ids` order."""
    index = {}
    for mod_id in mod_ids:
        root = _package_dir(me3_dir, mod_id)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                index.setdefault(path.relative_to(root).as_posix(), []).append(mod_id)
    return index


def apply_prunes(me3_dir, prunes):
    """Delete author-declared dead files. Returns the "mod:path" strings removed.

    A missing file is not an error: the mod may have stopped shipping it, which
    is the outcome the prune was asking for.
    """
    removed = []
    for entry in prunes:
        mod_id = entry["mod"]
        for rel in entry["paths"]:
            # `rel` comes straight out of a profile TOML, not the filesystem --
            # unlike index_paths's rglob results, nothing has confirmed it stays
            # under the package dir. A stray leading `../` (typo or otherwise)
            # would otherwise delete a file elsewhere on disk.
            if not is_safe_relpath(rel):
                raise ConflictError(f"unsafe prune path (refusing to delete): {rel}")
            target = _package_dir(me3_dir, mod_id) / rel
            if target.is_file():
                target.unlink()
                removed.append(f"{mod_id}:{rel}")
    return removed


def resolve(me3_dir, mod_ids, merges):
    """Merge every declared conflict and refuse any undeclared one.

    Merged output goes to a synthetic package and the path is removed from its
    sources, so the merged file is the only one providing it. That sidesteps
    me3's load order entirely rather than trusting it to break the tie our way.
    """
    me3_dir = Path(me3_dir)
    declared = {m["path"]: m for m in merges}
    index = index_paths(me3_dir, mod_ids)

    merged_paths = []
    for rel, providers in sorted(index.items()):
        if len(providers) < 2:
            continue
        spec = declared.get(rel)
        if spec is None:
            raise ConflictError(
                f"{rel} is provided by {', '.join(providers)} — me3 loads only one "
                f"of them, so the rest silently won't be in the game. Declare a "
                f"[[merges]] entry for this path, or drop one of the mods.")
        strategy = STRATEGIES.get(spec["strategy"])
        if strategy is None:
            raise ConflictError(
                f"{rel}: unknown merge strategy {spec['strategy']!r} "
                f"(known: {', '.join(sorted(STRATEGIES))})")
        prefer = spec["prefer"]
        if prefer not in providers:
            # A typo'd or stale `prefer` would otherwise reach read_bytes() below
            # and fail as a raw FileNotFoundError pointing at an internal path --
            # correct behind-the-scenes, but useless to whoever wrote the profile.
            raise ConflictError(
                f"{rel}: merge names prefer={prefer!r}, but the actual providers "
                f"are {', '.join(providers)} — prefer must be one of them")
        # `prefer` wins genuine collisions, so it's the base the strategy builds on.
        ordered = [prefer] + [m for m in providers if m != prefer]
        blobs = [(_package_dir(me3_dir, m) / rel).read_bytes() for m in ordered]
        out = blobs[0]
        for extra in blobs[1:]:
            out = strategy(out, extra)

        target = _package_dir(me3_dir, MERGED_ID) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out)
        for mod_id in providers:
            (_package_dir(me3_dir, mod_id) / rel).unlink()
        merged_paths.append(rel)
    return merged_paths


def clear_merged(me3_dir):
    """Drop the synthetic package so a re-apply rebuilds it from scratch."""
    shutil.rmtree(_package_dir(me3_dir, MERGED_ID), ignore_errors=True)
