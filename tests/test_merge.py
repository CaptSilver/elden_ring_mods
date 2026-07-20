import pytest

from ermlib import merge
from ermlib.formats import bnd4, dcx, fmg
from tests.test_bnd4 import _synthetic_bnd4


def _msgbnd(fmgs):
    """fmgs: {entry_id: {text_id: str}} -> a .msgbnd.dcx blob."""
    entries = [(eid, f"{eid}.fmg", fmg.write(table)) for eid, table in sorted(fmgs.items())]
    return dcx.write_dflt(_synthetic_bnd4(entries))


def _read_back(blob):
    return {e.id: fmg.read(e.data) for e in bnd4.read(dcx.read(blob))}


def test_disjoint_ids_from_both_sides_survive():
    base = _msgbnd({1: {100: "Clever's weapon"}})
    other = _msgbnd({1: {200: "Resurrect", 201: "Teleport"}})
    assert _read_back(merge.fmg_union(base, other)) == {
        1: {100: "Clever's weapon", 200: "Resurrect", 201: "Teleport"}}


def test_base_wins_a_genuine_collision():
    """Clever's overwrites one vanilla string with its own diagnostic note. The
    base side is the one we keep."""
    base = _msgbnd({1: {401106: "!!! MOD NOTE !!!"}})
    other = _msgbnd({1: {401106: "Failed to save."}})
    assert _read_back(merge.fmg_union(base, other))[1][401106] == "!!! MOD NOTE !!!"


def test_entries_only_in_base_are_untouched():
    base = _msgbnd({1: {1: "a"}, 2: {5: "only in base"}})
    other = _msgbnd({1: {2: "b"}})
    merged = _read_back(merge.fmg_union(base, other))
    assert merged[2] == {5: "only in base"}
    assert merged[1] == {1: "a", 2: "b"}


def test_output_is_dflt_compressed():
    base = _msgbnd({1: {1: "a"}})
    other = _msgbnd({1: {2: "b"}})
    assert merge.fmg_union(base, other)[0x28:0x2c] == b"DFLT"


def test_an_entry_only_in_other_is_refused():
    """The structural clone can't add BND4 entries. Refusing loudly beats
    dropping the extra entry and reporting success."""
    base = _msgbnd({1: {1: "a"}})
    other = _msgbnd({1: {2: "b"}, 99: {3: "new entry"}})
    with pytest.raises(merge.MergeError):
        merge.fmg_union(base, other)


def test_strategy_is_registered_by_name():
    assert merge.STRATEGIES["fmg-union"] is merge.fmg_union
