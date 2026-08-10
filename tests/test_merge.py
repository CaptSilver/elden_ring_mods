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


def _tpf_blob(textures):
    """textures: [(name, data)] -> a .tpf.dcx blob."""
    from ermlib.formats import tpf
    return dcx.write_dflt(tpf.write(tpf.Archive(
        tuple(tpf.Texture(n, d, 102, 0, 1, 0) for n, d in textures),
        platform=0, flag2=3, encoding=1)))


def _tpf_names(blob):
    from ermlib.formats import tpf
    return {t.name: t.data for t in tpf.read(dcx.read(blob)).textures}


def test_tpf_union_adds_the_other_side_s_new_textures():
    """Great Rune Overhaul's whole contribution to the menu atlas is one new
    texture. Without it the game still draws the icon its params reference and
    gets a missing-texture placeholder — the green squares."""
    base = _tpf_blob([("SB_Icon_02_A", b"clevers-icons")])
    other = _tpf_blob([("SB_Icon_02_A", b"vanilla-icons"),
                       ("SB_Status_GRO_00", b"rune-status")])
    out = _tpf_names(merge.tpf_union(base, other))
    assert out == {"SB_Icon_02_A": b"clevers-icons", "SB_Status_GRO_00": b"rune-status"}


def test_tpf_union_keeps_the_base_copy_of_a_shared_texture():
    """Both mods ship the whole ~204 MB atlas set, so nearly every texture is
    shared and identical. Where they differ, the preferred mod's copy is the one
    with its own icons layered in — taking the other's would erase them."""
    base = _tpf_blob([("SB_Icon_02_A", b"clevers-with-weapon-icons")])
    other = _tpf_blob([("SB_Icon_02_A", b"plain-vanilla")])
    assert _tpf_names(merge.tpf_union(base, other)) == {
        "SB_Icon_02_A": b"clevers-with-weapon-icons"}


# --- esd-3way: BND4 container level, esdmerge next door does the graph work ---


def _msgbnd_like(entries):
    """A BND4-in-DCX container holding raw blobs under the given entry ids."""
    return dcx.write_dflt(_synthetic_bnd4(
        [(eid, f"t{eid:09d}.esd", blob) for eid, blob in sorted(entries.items())]))


def test_esd_three_way_is_registered_and_needs_vanilla():
    assert "esd-3way" in merge.STRATEGIES
    assert "esd-3way" in merge.NEEDS_VANILLA


def test_esd_three_way_takes_an_entry_only_one_side_changed():
    """Melina's second edit, t000003000.esd, collides with nothing. It must come
    across without the ESD merge being involved at all."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({1: [(0, None)]}))
    changed = esd.write(_esd({1: [(0, None), (1, 0)]}))
    vanilla = _msgbnd_like({0: van, 6: van})
    base = _msgbnd_like({0: van, 6: van})
    other = _msgbnd_like({0: van, 6: changed})
    out = merge.esd_three_way(base, other, vanilla)
    entries = {e.id: e.data for e in bnd4.read(dcx.read(out))}
    assert entries[6] == changed
    assert entries[0] == van


def test_esd_three_way_refuses_an_entry_only_the_other_side_has():
    """The container clone can't add BND4 entries any more than fmg_union can --
    refusing loudly beats silently dropping the extra .esd."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({1: [(0, None)]}))
    vanilla = _msgbnd_like({0: van})
    base = _msgbnd_like({0: van})
    other = _msgbnd_like({0: van, 9: van})
    with pytest.raises(merge.MergeError, match="9"):
        merge.esd_three_way(base, other, vanilla)


def test_esd_three_way_leaves_an_untouched_entry_byte_identical():
    """An entry neither side changed must be copied through unrebuilt, not
    re-serialised through esd.read/write -- that's the same reasoning fmg-3way
    already applies to untouched FMG entries."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({1: [(0, None)]}))
    vanilla = _msgbnd_like({0: van})
    base = _msgbnd_like({0: van})
    other = _msgbnd_like({0: van})
    out = merge.esd_three_way(base, other, vanilla)
    assert out == base


def test_esd_three_way_runs_the_graph_merge_on_an_entry_both_sides_changed():
    """The one group both mods edit is the whole reason esdmerge exists at all;
    an entry where that happens has to actually reach it, not just the
    container-level wholesale swap."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    base_esd = esd.write(_esd({24: [(0, 2), (1, None), (2, None)]}))
    other_esd = esd.write(_esd({24: [(0, 3), (1, None), (2, None), (3, None)]}))
    vanilla = _msgbnd_like({6: van})
    base = _msgbnd_like({6: base_esd})
    other = _msgbnd_like({6: other_esd})

    out = merge.esd_three_way(base, other, vanilla)
    merged = esd.read({e.id: e.data for e in bnd4.read(dcx.read(out))}[6])
    group = next(g for g in merged.groups if g.id == 24)
    state0 = next(s for s in group.states if s.id == 0)
    assert [c.target for c in state0.conditions] == [2]     # base's redirect wins


def test_esd_three_way_collects_notes_when_the_graph_merge_leaves_one():
    """Notes must come out of the container adapter, not be swallowed -- that's
    the only way an apply can tell the user a mod's change didn't land."""
    from ermlib import esdmerge
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    base_esd = esd.write(_esd({24: [(0, 2), (1, None), (2, None)]}))
    other_esd = esd.write(_esd({24: [(0, 3), (1, None), (2, None), (3, None)]}))
    vanilla = _msgbnd_like({6: van})
    base = _msgbnd_like({6: base_esd})
    other = _msgbnd_like({6: other_esd})

    notes = []
    merge.esd_three_way(base, other, vanilla, notes=notes)
    # Both mods redirect state 0 differently, so base's version wins and that's
    # the note this test is after. The other mod's own new state 3 also arrives
    # orphaned as a result (nothing still points at it) -- a second, correct note
    # from the same conflict, not something this test needs to pin down further.
    assert any(n.group == 24 and n.state == 0 and n.reason is esdmerge.Reason.BOTH_CHANGED
               for n in notes), notes


def test_esd_three_way_without_a_notes_list_does_not_raise():
    """conflicts.resolve() only threads a notes list through for strategies
    that ask for it; called bare (as the registration tests above do), the
    strategy must not require one."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    base_esd = esd.write(_esd({24: [(0, 2), (1, None), (2, None)]}))
    other_esd = esd.write(_esd({24: [(0, 3), (1, None), (2, None), (3, None)]}))
    vanilla = _msgbnd_like({6: van})
    base = _msgbnd_like({6: base_esd})
    other = _msgbnd_like({6: other_esd})
    merge.esd_three_way(base, other, vanilla)          # must not raise


def test_tpf_union_preserves_the_order_and_flags_of_every_texture():
    """Appending must not disturb the existing table: the game reads textures by
    offset, and the entries carry per-texture format/mipmap flags that aren't
    interchangeable between a DDS atlas and an icon sheet."""
    from ermlib.formats import tpf
    base = dcx.write_dflt(tpf.write(tpf.Archive(
        (tpf.Texture("A", b"aaaa", 102, 0, 3, 1), tpf.Texture("B", b"bb", 0, 0, 1, 0)),
        platform=0, flag2=3, encoding=1)))
    other = _tpf_blob([("C", b"cc")])
    out = tpf.read(dcx.read(merge.tpf_union(base, other))).textures
    assert [t.name for t in out] == ["A", "B", "C"]
    assert (out[0].format, out[0].mipmaps, out[0].flags1) == (102, 3, 1)
    assert (out[1].format, out[1].mipmaps, out[1].flags1) == (0, 1, 0)
