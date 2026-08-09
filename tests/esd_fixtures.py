"""The three real t000001000.esd blobs, pulled from the vendor archives.

Extracted rather than checked in: they are 200-400 KB each and the archives are
already pinned in mods.lock.toml. Tests skip when vendor/ isn't populated.
"""
import zipfile
from pathlib import Path

import pytest

from ermlib.formats import bnd4, dcx

VENDOR = Path("vendor")

_SOURCES = {
    "vanilla": ("Elden Ring Randomizer-428-v0-11-4-1763103112.zip",
                "randomizer/diste/Vanilla/m00_00_00_00.talkesdbnd.dcx"),
    "bossres": ("Boss Resurrection - Lite-2790-2-0-1-1720450846.zip",
                "Mod-Lite/script/talk/m00_00_00_00.talkesdbnd.dcx"),
    "melina": ("Journey With Melina V1.1 10079 1.1 2026-06-12T09-51Z n7XrmU9K3.zip",
               "script/talk/m00_00_00_00.talkesdbnd.dcx"),
}


def talkesd_container(which):
    """The whole .talkesdbnd.dcx blob for one side."""
    archive, member = _SOURCES[which]
    path = VENDOR / archive
    if not path.exists():
        pytest.skip(f"vendor archive missing: {archive}")
    with zipfile.ZipFile(path) as z:
        return z.read(member)


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
