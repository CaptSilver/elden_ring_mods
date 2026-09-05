"""The ESD fixtures have to read what apply reads.

`erm fetch --update` rewrites an asset line and leaves the superseded archive in
vendor/, so a fixture that names archives by hand goes on certifying a blob the
stack no longer merges -- green, and meaningless. These pin the resolution to
the lockfile and the merge declaration instead.
"""
import zipfile
from pathlib import Path

import pytest

from ermlib.manifest import load_lock
from tests import esd_fixtures


def _lock_with(mod_id, asset):
    lock = dict(load_lock("mods.lock.toml"))
    lock[mod_id] = dict(lock[mod_id], asset=asset)
    return lock


def test_a_repin_the_fixtures_have_not_followed_skips_rather_than_reading_the_old_blob(monkeypatch):
    """The superseded archive is still sitting in vendor/, so "resolve by name"
    reads it happily. Resolving through the lock means an unfetched repin says
    so."""
    monkeypatch.setattr(esd_fixtures, "load_lock",
                        lambda *a: _lock_with("boss-resurrection-lite",
                                              "not-fetched-yet.zip"))
    with pytest.raises(pytest.skip.Exception, match="not-fetched-yet.zip"):
        esd_fixtures.talkesd_container("bossres")


def test_a_release_that_moved_the_file_fails_instead_of_guessing(tmp_path, monkeypatch):
    """Two candidates is a layout change, not a coin toss -- picking one would
    put the merge fixtures on an arbitrary blob."""
    archive = tmp_path / "repinned.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(f"Mod-Lite/{esd_fixtures.MERGE_PATH}", b"a")
        z.writestr(f"Mod-Full/{esd_fixtures.MERGE_PATH}", b"b")
    monkeypatch.setattr(esd_fixtures, "VENDOR", tmp_path)
    monkeypatch.setattr(esd_fixtures, "load_lock",
                        lambda *a: _lock_with("journey-with-melina", archive.name))
    with pytest.raises(LookupError, match="expected one"):
        esd_fixtures.talkesd_container("melina")


def test_a_backup_sibling_is_not_mistaken_for_the_merged_file():
    """Boss Res ships a Smithbox .bak of this exact path; the suffix match has
    to end at the .dcx or the fixtures would have two hits every release."""
    names = [f"Mod-Lite/{esd_fixtures.MERGE_PATH}",
             f"Mod-Lite/{esd_fixtures.MERGE_PATH}.bak"]
    assert esd_fixtures._member(names, "x.zip") == names[0]


def test_the_vanilla_side_is_loaded_by_the_code_the_merge_uses(monkeypatch):
    """Same ancestor loader as apply, so the fixture and the merge cannot judge
    different vanilla bytes."""
    seen = {}

    def fake_ancestor(merges, rel, lock):
        seen["rel"] = rel
        seen["paths"] = [m["path"] for m in merges]
        return b"ancestor bytes"

    monkeypatch.setattr(esd_fixtures.conflicts, "declared_ancestor", fake_ancestor)
    assert esd_fixtures.talkesd_container("vanilla") == b"ancestor bytes"
    assert seen["rel"] == esd_fixtures.MERGE_PATH
    assert seen["paths"] == [esd_fixtures.MERGE_PATH]


def test_the_fixtures_supply_every_mod_the_profile_merges(monkeypatch):
    """A third mod joining the merge would leave a side of it untested, and the
    fixtures are where that has to show up."""
    merge, lock = esd_fixtures._merge_and_lock()
    grown = dict(merge, mods=list(merge["mods"]) + ["some-new-mod"])
    monkeypatch.setattr(esd_fixtures, "load_profile",
                        lambda *a, **k: {"merges": [grown]})
    with pytest.raises(AssertionError, match="some-new-mod"):
        esd_fixtures._merge_and_lock()
