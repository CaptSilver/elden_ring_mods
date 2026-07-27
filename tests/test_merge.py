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


# --- param-rows: transplant one mod's param edits onto another's regulation ---

from ermlib.formats import regulation                          # noqa: E402
from tests.test_param import make_param                        # noqa: E402


def _regulation(params):
    """params: {entry_id: (param_type, {row_id: row_bytes}, stride)}"""
    entries = []
    for eid, (ptype, rows, stride) in sorted(params.items()):
        blob = make_param(sorted(rows), stride=stride, param_type=ptype,
                          fill=lambda rid, rows=rows: rows[rid])
        entries.append((eid, f"{ptype.decode()}.param", blob))
    return regulation.pack(_synthetic_bnd4(entries), bytes(16))


def _rows(blob, eid):
    from ermlib.formats import param
    entry = next(e for e in regulation.entries(blob) if e.id == eid)
    return {r.id: r.data for r in param.read(entry.data).rows}


SP = b"SP_EFFECT_PARAM_ST"


def test_param_rows_takes_the_other_sides_edit_when_base_matches_vanilla():
    van = _regulation({1: (SP, {100: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {100: b"\x00" * 8}, 8)})
    other = _regulation({1: (SP, {100: b"\xff" * 8}, 8)})
    assert _rows(merge.param_rows(base, other, van), 1)[100] == b"\xff" * 8


def test_param_rows_keeps_the_base_edit_when_other_matches_vanilla():
    """Row 174 in the real data: Clever's changed effectEndurance, the other mod
    still carries vanilla's value. A whole-param copy would revert Clever's."""
    van = _regulation({1: (SP, {174: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {174: b"\xbb" * 8}, 8)})
    other = _regulation({1: (SP, {174: b"\x00" * 8}, 8)})
    assert _rows(merge.param_rows(base, other, van), 1)[174] == b"\xbb" * 8


def test_param_rows_inserts_a_row_neither_base_nor_vanilla_has():
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x01" * 8, 360401: b"\xaa" * 8}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert out[360401] == b"\xaa" * 8
    assert out[1] == b"\x01" * 8


def test_param_rows_refuses_when_both_sides_changed_the_same_row():
    van = _regulation({1: (SP, {5: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {5: b"\x11" * 8}, 8)})
    other = _regulation({1: (SP, {5: b"\x22" * 8}, 8)})
    with pytest.raises(merge.MergeError, match="row 5"):
        merge.param_rows(base, other, van)


def test_param_rows_leaves_an_untouched_param_byte_identical():
    """A param the other side didn't change must be copied through, not
    re-serialised: byte-comparing params over-reports differences roughly
    threefold because tools disagree on the strings-offset rounding."""
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8), 2: (b"OTHER_ST", {9: b"\x09" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x01" * 8}, 8), 2: (b"OTHER_ST", {9: b"\xcc" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x22" * 8}, 8), 2: (b"OTHER_ST", {9: b"\x09" * 8}, 8)})
    out = merge.param_rows(base, other, van)
    before = {e.id: e.data for e in regulation.entries(base)}
    after = {e.id: e.data for e in regulation.entries(out)}
    assert after[2] == before[2]
    assert after[1] != before[1]


def test_param_rows_tolerates_a_stride_that_only_differs_by_trailing_padding():
    """Ten real params derive a different stride per file because vanilla's
    writer emits trailing padding SoulsFormats does not. The content over the
    shorter row is identical, so there is no change to merge."""
    van = _regulation({1: (SP, {1: b"\xab" * 8 + b"\x00" * 4}, 12)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    out = merge.param_rows(base, other, van)
    before = {e.id: e.data for e in regulation.entries(base)}
    assert {e.id: e.data for e in regulation.entries(out)}[1] == before[1]


def test_param_rows_transplants_when_only_vanilla_has_the_padding():
    # Vanilla carries trailing padding both mods strip, and the other side made
    # a real edit. Base and other agree on stride, so the row transplants fine —
    # vanilla's width only affects the comparison, not what can be written.
    van = _regulation({1: (SP, {1: b"\xab" * 8 + b"\x00" * 4}, 12)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x99" * 8}, 8)})
    assert _rows(merge.param_rows(base, other, van), 1)[1] == b"\x99" * 8


def test_param_rows_refuses_a_row_it_cannot_fit_into_the_base():
    # Base and other genuinely disagree on row width, so the other side's bytes
    # have no meaning in the base's layout. Writing them anyway would corrupt
    # the row silently; a paramdef would be needed to reinterpret it.
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x99" * 12}, 12)})
    with pytest.raises(merge.MergeError, match="stride"):
        merge.param_rows(base, other, van)


def test_param_rows_output_is_a_loadable_regulation():
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x02" * 8}, 8)})
    out = merge.param_rows(base, other, van)
    assert regulation.unpack(out)[:4] == b"BND4"
    assert len(regulation.entries(out)) == 1


def test_param_rows_skips_an_entry_both_mods_already_agree_on():
    """Three of vanilla's own entries are unreadable until a tool re-saves them
    (FromSoft stores a strings offset past the end of the file). Both mods ship
    the normalised copy, identical to each other, so there is nothing to merge
    and nothing that needs parsing."""
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x02" * 8}, 8)})
    other = base
    out = merge.param_rows(base, other, van)
    before = {e.id: e.data for e in regulation.entries(base)}
    assert {e.id: e.data for e in regulation.entries(out)}[1] == before[1]


# --- intra-row merging: two mods editing different fields of one row ---


def test_param_rows_merges_two_mods_editing_different_fields_of_a_row():
    """Row-granular merging calls this a conflict, but a field is a contiguous
    byte range: disjoint byte edits are disjoint field edits. Clever's Moveset
    and a weapon-buff mod hit 39 of the same EQUIP_PARAM_WEAPON rows and never
    the same byte -- the buff mod only ever sets one flag byte."""
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: b"\xaa" + bytes(7)}, 8)})          # byte 0
    other = _regulation({1: (SP, {5: bytes(4) + b"\xbb" + bytes(3)}, 8)})  # byte 4
    out = _rows(merge.param_rows(base, other, van), 1)[5]
    assert out == b"\xaa" + bytes(3) + b"\xbb" + bytes(3)


def test_param_rows_still_refuses_when_both_change_the_same_byte():
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: b"\xaa" + bytes(7)}, 8)})
    other = _regulation({1: (SP, {5: b"\xbb" + bytes(7)}, 8)})
    with pytest.raises(merge.MergeError, match="row 5"):
        merge.param_rows(base, other, van)


def test_param_rows_accepts_both_sides_making_the_identical_field_edit():
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: b"\xaa" + bytes(7)}, 8)})
    other = _regulation({1: (SP, {5: b"\xaa" + bytes(7)}, 8)})
    assert _rows(merge.param_rows(base, other, van), 1)[5] == b"\xaa" + bytes(7)


def test_param_rows_keeps_an_untouched_field_from_vanilla():
    # Neither side touches byte 2; it must stay vanilla's value, not drift.
    van = _regulation({1: (SP, {5: b"\x00\x00\x77\x00" + bytes(4)}, 8)})
    base = _regulation({1: (SP, {5: b"\xaa\x00\x77\x00" + bytes(4)}, 8)})
    other = _regulation({1: (SP, {5: b"\x00\x00\x77\x00" + bytes(3) + b"\xbb"}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)[5]
    assert out == b"\xaa\x00\x77\x00" + bytes(3) + b"\xbb"


# --- folding one merge across three or more contributors ---


def test_param_rows_folds_across_three_mods_with_vanilla_constant():
    """conflicts.resolve() folds pairwise, so N contributors means N-1 merges
    with vanilla as the reference every time. Each mod's rows have to survive
    the folds that come after it."""
    van = _regulation({1: (SP, {1: bytes(8), 2: bytes(8), 3: bytes(8)}, 8)})
    base = _regulation({1: (SP, {1: b"\xaa" * 8, 2: bytes(8), 3: bytes(8)}, 8)})
    second = _regulation({1: (SP, {1: bytes(8), 2: b"\xbb" * 8, 3: bytes(8)}, 8)})
    third = _regulation({1: (SP, {1: bytes(8), 2: bytes(8), 3: b"\xcc" * 8}, 8)})
    out = _rows(merge.param_rows(merge.param_rows(base, second, van), third, van), 1)
    assert out[1] == b"\xaa" * 8
    assert out[2] == b"\xbb" * 8
    assert out[3] == b"\xcc" * 8


def test_param_rows_catches_a_clash_between_two_later_contributors():
    """The base starts out agreeing with vanilla here, so this clash is between
    the second and third mods. Folding is what makes it visible: the second
    mod's edit becomes part of the base the third is compared against."""
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: bytes(8)}, 8)})
    second = _regulation({1: (SP, {5: b"\xbb" + bytes(7)}, 8)})
    third = _regulation({1: (SP, {5: b"\xcc" + bytes(7)}, 8)})
    with pytest.raises(merge.MergeError, match="byte 0"):
        merge.param_rows(merge.param_rows(base, second, van), third, van)


def test_param_rows_folds_three_mods_editing_different_fields_of_one_row():
    """Three-way field-level composition: each mod owns a different byte of the
    same row, and all three edits have to end up in the result."""
    van = _regulation({1: (SP, {7: bytes(8)}, 8)})
    base = _regulation({1: (SP, {7: b"\x01" + bytes(7)}, 8)})
    second = _regulation({1: (SP, {7: bytes(3) + b"\x02" + bytes(4)}, 8)})
    third = _regulation({1: (SP, {7: bytes(6) + b"\x03" + bytes(1)}, 8)})
    out = _rows(merge.param_rows(merge.param_rows(base, second, van), third, van), 1)
    assert out[7] == b"\x01\x00\x00\x02\x00\x00\x03\x00"
