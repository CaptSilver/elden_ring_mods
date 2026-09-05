"""The three real t000001000.esd blobs, pulled from the vendor archives.

Extracted rather than checked in: they are 200-400 KB each and the archives are
already pinned in mods.lock.toml. Tests skip when vendor/ isn't populated.

Which archive, and which member inside it, are resolved the same way `erm apply`
resolves them -- the merge declared in profiles/gameplay-extras.toml plus the
assets in mods.lock.toml. Naming them here by hand made a repin silent: `erm
fetch --update` rewrites the asset line and nothing prunes vendor/, so the
superseded zip stays on disk and the fixtures keep reading it while apply merges
the new one. EXPECTED below is the one thing still written out, because those
counts are properties of the blobs -- repinning any of these three mods means
re-deriving them, and they will fail loudly until you do.
"""
import zipfile
from pathlib import Path

import pytest

from ermlib import conflicts
from ermlib.formats import bnd4, dcx
from ermlib.manifest import load_lock, load_profile

VENDOR = Path("vendor")
MERGE_PATH = "script/talk/m00_00_00_00.talkesdbnd.dcx"

# The two mod sides, keyed by the short name the tests use. Mod ids are stable
# across releases where the asset filenames are not, which is the point.
MOD_IDS = {"bossres": "boss-resurrection-lite",
           "melina": "journey-with-melina"}


def _merge_and_lock():
    merge = next(m for m in load_profile("gameplay-extras",
                                         base=Path("profiles"))["merges"]
                 if m["path"] == MERGE_PATH)
    assert set(MOD_IDS.values()) == set(merge["mods"]), (
        f"the profile now merges {merge['mods']} for {MERGE_PATH}; these "
        f"fixtures still supply {sorted(MOD_IDS.values())}")
    return merge, load_lock("mods.lock.toml")


def _archive(mod_id, lock):
    asset = lock.get(mod_id, {}).get("asset")
    if not asset:
        pytest.skip(f"{mod_id} has no archive pinned in mods.lock.toml")
    path = VENDOR / asset
    if not path.exists():
        pytest.skip(f"vendor archive missing: {asset}")
    return path


def _member(names, asset):
    """The one member that is the merged file.

    Matched rather than hardcoded: each mod wraps it in its own directory and
    those wrappers move between releases. Anything but exactly one hit is a
    layout change worth failing on rather than guessing at. Boss Res's Smithbox
    ".bak" sibling doesn't match -- the suffix ends at the .dcx.
    """
    hits = [n for n in names if n == MERGE_PATH or n.endswith("/" + MERGE_PATH)]
    if len(hits) != 1:
        raise LookupError(
            f"{asset}: expected one {MERGE_PATH}, found {hits} -- the archive's "
            f"layout moved, so re-derive the fixtures against the new release")
    return hits[0]


def talkesd_container(which):
    """The whole .talkesdbnd.dcx blob for one side."""
    merge, lock = _merge_and_lock()
    if which == "vanilla":
        # Production's own ancestor loader, so the fixture and the merge it
        # exercises can never end up judging different vanilla bytes.
        asset = lock.get(merge["vanilla"]["mod"], {}).get("asset")
        if not asset or not (VENDOR / asset).exists():
            pytest.skip(f"vendor archive missing: {asset}")
        return conflicts.declared_ancestor([merge], MERGE_PATH, lock)
    path = _archive(MOD_IDS[which], lock)
    with zipfile.ZipFile(path) as z:
        return z.read(_member(z.namelist(), path.name))


def real_esd(which):
    """t000001000.esd — BND4 entry 0 — for one side."""
    entries = {e.id: e for e in bnd4.read(dcx.read(talkesd_container(which)))}
    return entries[0].data


EXPECTED = {
    # (group count, state count, condition count, command call count, arg count)
    "vanilla": (86, 1024, 1092, 666, 1444),
    "bossres": (136, 1650, 2076, 1602, 3465),
    "melina": (87, 1032, 1208, 669, 1447),
}
