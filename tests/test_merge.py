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


def test_base_none_defers_to_others_real_text():
    """A base id with no text (None) isn't an authored value -- it shouldn't
    clobber a real string the other side has for that id."""
    base = _msgbnd({1: {5: None, 6: "kept"}})
    other = _msgbnd({1: {5: "real text from other"}})
    assert _read_back(merge.fmg_union(base, other))[1] == {5: "real text from other", 6: "kept"}


def test_strategy_is_registered_by_name():
    assert merge.STRATEGIES["fmg-union"] is merge.fmg_union


# --- fmg-3way: two-way union can't tell an authored value from an unchanged one ---


def test_three_way_takes_the_other_sides_edit_when_base_matches_vanilla():
    van = _msgbnd({1: {10: "vanilla text"}})
    base = _msgbnd({1: {10: "vanilla text"}})
    other = _msgbnd({1: {10: "modded text"}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][10] == "modded text"


def test_three_way_keeps_the_base_edit_when_other_matches_vanilla():
    """The failure mode of prefer=other under two-way union: the other side
    holds unchanged vanilla text, which outranks the base's authored edit."""
    van = _msgbnd({1: {10: "vanilla text"}})
    base = _msgbnd({1: {10: "base's authored edit"}})
    other = _msgbnd({1: {10: "vanilla text"}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][10] == "base's authored edit"


def test_three_way_refuses_when_both_sides_edited_the_same_row():
    van = _msgbnd({1: {10: "vanilla"}})
    base = _msgbnd({1: {10: "base version"}})
    other = _msgbnd({1: {10: "other version"}})
    with pytest.raises(merge.MergeError, match="1.*10"):
        merge.fmg_three_way(base, other, van)


def test_three_way_allows_both_sides_making_the_identical_edit():
    van = _msgbnd({1: {10: "vanilla"}})
    base = _msgbnd({1: {10: "same new text"}})
    other = _msgbnd({1: {10: "same new text"}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][10] == "same new text"


def test_three_way_does_not_resurrect_a_row_the_base_deleted():
    """The 1,062-row bug in miniature. Under two-way union a base id present
    with None looks identical to an absent id, so the other side's vanilla text
    refills a row the base deliberately emptied."""
    van = _msgbnd({1: {10: "vanilla text"}})
    base = _msgbnd({1: {10: None}})
    other = _msgbnd({1: {10: "vanilla text"}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][10] is None


def test_three_way_applies_a_deletion_the_other_side_authored():
    van = _msgbnd({1: {10: "vanilla text"}})
    base = _msgbnd({1: {10: "vanilla text"}})
    other = _msgbnd({1: {10: None}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][10] is None


def test_three_way_adds_a_row_neither_base_nor_vanilla_has():
    van = _msgbnd({1: {10: "a"}})
    base = _msgbnd({1: {10: "a"}})
    other = _msgbnd({1: {10: "a", 11: "brand new"}})
    assert _read_back(merge.fmg_three_way(base, other, van))[1][11] == "brand new"


def test_three_way_refuses_a_mismatched_entry_set():
    van = _msgbnd({1: {10: "a"}})
    base = _msgbnd({1: {10: "a"}})
    other = _msgbnd({1: {10: "a"}, 99: {1: "extra"}})
    with pytest.raises(merge.MergeError, match="entr"):
        merge.fmg_three_way(base, other, van)


def test_three_way_leaves_untouched_entries_byte_identical():
    """Rewriting an FMG is not byte-faithful -- fmg.write dedupes strings that
    vanilla stores duplicated -- so an entry nobody edited must be copied
    through, not re-serialised."""
    van = _msgbnd({1: {10: "a"}, 2: {20: "untouched"}})
    base = _msgbnd({1: {10: "a"}, 2: {20: "untouched"}})
    other = _msgbnd({1: {10: "changed"}, 2: {20: "untouched"}})
    out = merge.fmg_three_way(base, other, van)
    before = {e.id: e.data for e in bnd4.read(dcx.read(base))}
    after = {e.id: e.data for e in bnd4.read(dcx.read(out))}
    assert after[2] == before[2]
    assert after[1] != before[1]
