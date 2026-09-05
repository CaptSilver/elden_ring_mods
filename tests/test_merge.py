from unittest import mock

import pytest

from ermlib import esdmerge, merge
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


def _regulation(params, version=b"07D7R6\x00\x00"):
    """params: {entry_id: (param_type, {row_id: row_bytes}, stride)}"""
    entries = []
    for eid, (ptype, rows, stride) in sorted(params.items()):
        blob = make_param(sorted(rows), stride=stride, param_type=ptype,
                          fill=lambda rid, rows=rows: rows[rid])
        entries.append((eid, f"{ptype.decode()}.param", blob))
    return regulation.pack(_synthetic_bnd4(entries, version=version), bytes(16))


def _rows(blob, eid):
    from ermlib.formats import param
    entry = next(e for e in regulation.entries(blob) if e.id == eid)
    return {r.id: r.data for r in param.read(entry.data).rows}


def _entry(blob, eid):
    """One regulation entry's raw bytes, for assertions that must not go
    through the param reader."""
    return {e.id: e.data for e in regulation.entries(blob)}[eid]


SP = b"SP_EFFECT_PARAM_ST"


def _dup_regulation(params, version=b"07D7R6\x00\x00"):
    """Like _regulation, but rows are an ordered (id, bytes) list rather than a
    dict, so one param can carry an id twice. Shipped params do:
    RandomAppearParam has 5,322 rows for 5,296 unique ids."""
    entries = []
    for eid, (ptype, rows, stride) in sorted(params.items()):
        data = iter([d for _, d in rows])
        blob = make_param([rid for rid, _ in rows], stride=stride, param_type=ptype,
                          fill=lambda rid, data=data: next(data))
        entries.append((eid, f"{ptype.decode()}.param", blob))
    return regulation.pack(_synthetic_bnd4(entries, version=version), bytes(16))


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
    # the row silently; a paramdef would be needed to reinterpret it. Two rows,
    # because a width measured from a single row is padding, not a layout fact.
    van = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\xcd" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\xcd" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x99" * 12, 2: b"\xcd" * 12}, 12)})
    with pytest.raises(merge.MergeError, match="stride"):
        merge.param_rows(base, other, van)


def test_param_rows_transplants_into_a_single_row_param_the_game_padded_wider():
    """The base is the game's own file, where a one-row param's derived stride
    carries alignment padding a mod's re-saved copy doesn't have. The mod's
    edit still belongs in that row; the base keeps its own padding."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8 + b"\x00" * 4}, 12)})
    other = _regulation({1: (SP, {1: b"\x99" * 8}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert out[1] == b"\x99" * 8 + b"\x00" * 4


def test_param_rows_refuses_a_single_row_whose_extra_bytes_are_not_padding():
    """A wider incoming row is only safe to trim where the excess is padding.
    Non-zero bytes past the base's width are content, and dropping them would
    lose an edit without saying so."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x99" * 12}, 12)})
    with pytest.raises(merge.MergeError, match="padding"):
        merge.param_rows(base, other, van)


def test_param_rows_keeps_the_mods_row_when_the_game_and_a_mod_each_invent_one():
    """Rebasing onto a newer game build meets rows vanilla never had: the 1.17
    patch and a mod both claim ShopLineupParam 101896 with different bytes.
    There is no ancestor to locate either edit against, so renumbering the
    loser isn't an option either -- a shop row is reached by id range, and
    101880-101920 is a full block with no free slot to move it to. The mod's
    row wins and the drop is named so it reaches the apply report."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8, 101896: b"\x11" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8, 101896: b"\x22" * 8}, 8)})
    notes = []
    out = merge.param_rows(base, other, van, notes=notes, base_is_game=True)
    assert _rows(out, 1)[101896] == b"\x22" * 8
    assert notes == [merge.RowCollision(1, 101896)]


def test_param_rows_refuses_when_two_mods_each_invent_the_same_row():
    """Only the game gets its row dropped for a mod's. Between two mods there
    is no reason to prefer either, and picking the last one folded discards a
    mod's content with nothing but a note about a game blob that was never
    involved."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8, 500: b"\x11" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8, 500: b"\x22" * 8}, 8)})
    notes = []
    with pytest.raises(merge.MergeError, match="row 500"):
        merge.param_rows(base, other, van, notes=notes)
    assert notes == []


def test_param_rows_keeps_a_row_only_the_game_added():
    """18 of the 19 rows 1.17 added are in no mod's file at all. Losing them
    to the new collision rule would silently strip the patch's own content --
    the rule must only fire when a mod also claimed the same id."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8, 99: b"\x33" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    out = merge.param_rows(base, other, van, base_is_game=True)
    assert _rows(out, 1)[99] == b"\x33" * 8


def test_param_rows_still_inserts_a_row_only_the_mod_added():
    """The collision rule requires the game to have added a row under the same
    id too -- it must not intercept the ordinary new-row insert path."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8, 99: b"\x44" * 8}, 8)})
    out = merge.param_rows(base, other, van, base_is_game=True)
    assert _rows(out, 1)[99] == b"\x44" * 8


def test_param_rows_still_merges_a_genuine_conflict_when_vanilla_has_the_row():
    """The collision rule applies only when vanilla lacks the row entirely.
    Here vanilla has it, so there IS a common version to locate each side's
    edit against, and the ordinary byte-level merge must keep running instead
    -- disjoint field edits are not a collision either way."""
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: b"\xaa" + bytes(7)}, 8)})              # byte 0
    other = _regulation({1: (SP, {5: bytes(4) + b"\xbb" + bytes(3)}, 8)})  # byte 4
    notes = []
    out = merge.param_rows(base, other, van, notes=notes)
    assert _rows(out, 1)[5] == b"\xaa" + bytes(3) + b"\xbb" + bytes(3)
    assert notes == []


def test_describe_note_renders_a_row_collision():
    text = merge.describe_note(merge.RowCollision(145, 101896))
    assert "145" in text and "101896" in text
    assert "mod" in text.lower() and "drop" in text.lower()


def test_param_rows_refuses_when_the_base_dropped_a_row_the_other_side_edited():
    van = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\x77" * 8}, 8)})
    with pytest.raises(merge.MergeError, match="row 2"):
        merge.param_rows(base, other, van)


def test_param_rows_refuses_when_the_other_side_dropped_a_row_the_base_edited():
    van = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\x77" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    with pytest.raises(merge.MergeError, match="row 2"):
        merge.param_rows(base, other, van)


def test_param_rows_keeps_a_shared_drop_when_it_is_the_other_that_edited():
    """Neither side has the row any more, so there is nothing to reconcile --
    they agree. esd-3way and fmg-3way both treat a shared deletion that way,
    and calling it a conflict stops the whole apply over an agreement."""
    van = _regulation({1: (SP, {1: b"\x00" * 8, 5: b"\x55" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x00" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\xbb" * 8}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert 5 not in out
    assert out[1] == b"\xbb" * 8


def test_param_rows_keeps_a_shared_drop_when_it_is_the_base_that_edited():
    """Mirror of the above with the surviving edit on the other side of the
    fold -- the shared deletion must not become a conflict either way round."""
    van = _regulation({1: (SP, {1: b"\x00" * 8, 5: b"\x55" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xaa" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x00" * 8}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert 5 not in out
    assert out[1] == b"\xaa" * 8


def test_param_rows_names_the_entries_the_three_regulations_disagree_on():
    """The likeliest way to reach this is a game patch adding a param, and
    counts alone leave the user with nothing to act on -- they can't tell
    whether to update the mod, re-pin the profile's vanilla, or file a bug."""
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x01" * 8}, 8),
                        99: (b"NEW_PARAM_ST", {1: b"\x02" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x02" * 8}, 8)})
    with pytest.raises(merge.MergeError, match="entry 99"):
        merge.param_rows(base, other, van)


def test_param_rows_drops_a_row_only_the_other_side_deleted():
    """The one arm of this merge that removes content. Base still matches
    vanilla on that row, so the other side's deletion is the only edit and it
    has to reach the output."""
    van = _regulation({1: (SP, {1: b"\x11" * 8, 2: b"\x22" * 8, 3: b"\x33" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x11" * 8, 2: b"\x22" * 8, 3: b"\x33" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x11" * 8, 3: b"\x33" * 8}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert 2 not in out
    assert out[1] == b"\x11" * 8
    assert out[3] == b"\x33" * 8


def test_param_rows_deletes_and_inserts_in_the_same_pass():
    """A shrink and a grow in one param means the row table, the row data and
    the strings block all move by a net offset neither edit alone produces --
    and the result still has to reparse."""
    from ermlib.formats import param
    van = _regulation({1: (SP, {1: b"\x11" * 8, 2: b"\x22" * 8, 3: b"\x33" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x11" * 8, 2: b"\x22" * 8, 3: b"\x33" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x11" * 8, 3: b"\x33" * 8, 99: b"\x99" * 8}, 8)})
    merged = _entry(merge.param_rows(base, other, van), 1)
    reparsed = param.read(merged)
    assert [r.id for r in reparsed.rows] == [1, 3, 99]
    assert reparsed.param_type == SP
    assert param.write(reparsed) == merged


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


def test_param_rows_accepts_both_sides_making_the_identical_edit():
    """NoFallDead's regulation is built on top of Clever's, so both carry the
    same added row. The shared edit has to sit on a row vanilla lacks for this
    to pin anything: with an ancestor row present the byte merge reaches the
    same answer on its own, and without the same-edit arm the added row has
    nothing to locate against and the fold refuses outright."""
    van = _regulation({1: (SP, {5: bytes(8)}, 8)})
    base = _regulation({1: (SP, {5: bytes(8), 900: b"\xaa" + bytes(7)}, 8)})
    other = _regulation({1: (SP, {5: b"\xbb" + bytes(7), 900: b"\xaa" + bytes(7)}, 8)})
    out = _rows(merge.param_rows(base, other, van), 1)
    assert out[900] == b"\xaa" + bytes(7)
    assert out[5] == b"\xbb" + bytes(7)


def test_param_rows_trims_a_transplanted_row_to_the_bases_narrower_layout():
    """Mirror of the non-padding refusal: where the excess past the base's
    width really is padding, the row fits and the transplant goes through
    rather than blocking the merge."""
    van = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\xab" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x99" * 8 + bytes(4)}, 12)})
    assert _rows(merge.param_rows(base, other, van), 1)[1] == b"\x99" * 8


def test_param_rows_refuses_a_row_both_sides_changed_at_different_widths():
    """Three versions of differing width give the byte merge nothing to line
    up: zip() would stop at the shortest and drop the wider side's tail into
    regulation.bin without a word."""
    van = _regulation({1: (SP, {1: b"\xab" * 8, 2: b"\xcd" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x11" * 12, 2: b"\xcd" * 8 + bytes(4)}, 12)})
    other = _regulation({1: (SP, {1: b"\x22" * 8, 2: b"\xcd" * 8}, 8)})
    with pytest.raises(merge.MergeError, match="different widths"):
        merge.param_rows(base, other, van)


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


def test_esd_three_way_refuses_an_entry_vanilla_does_not_have():
    """Without vanilla to compare an entry both sides carry against, there is
    no way to tell "only the other side moved" from "base authored this and
    vanilla simply doesn't happen to have it" -- picking one silently is how
    the preferred mod's own content gets thrown away. fmg_three_way and
    param_rows both refuse on any entry-set mismatch; esd-3way has to match,
    not just check the direction that happens to be exercised by the extra-
    entries test above."""
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({1: [(0, None)]}))
    base_only = esd.write(_esd({24: [(0, 1), (1, None)]}))
    other_only = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    vanilla = _msgbnd_like({0: van})                       # no entry 6 at all
    base = _msgbnd_like({0: van, 6: base_only})
    other = _msgbnd_like({0: van, 6: other_only})
    with pytest.raises(merge.MergeError, match="6"):
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
    # The entry's own bytes are the claim. Comparing whole containers passes
    # for a second reason -- the fixture is already DFLT-compressed the way
    # rebuild writes it -- so it would go on passing if the entry were
    # re-serialised into byte-identical DCX.
    assert {e.id: e.data for e in bnd4.read(dcx.read(out))}[0] == van


def _esd_entries(blob):
    return {e.id: e.data for e in bnd4.read(dcx.read(blob))}


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
    merged = esd.read(_esd_entries(out)[6])
    group = next(g for g in merged.groups if g.id == 24)
    state0 = next(s for s in group.states if s.id == 0)
    assert [c.target for c in state0.conditions] == [2]     # base's redirect wins


def test_esd_three_way_writes_the_merged_graph_into_the_entry():
    """What ships is the entry's bytes, and nothing else here asserts on them.

    The graph merge can run, hand back a correct graph and collect its notes
    while the adapter forgets to write the result -- base's blob goes out
    unchanged, every note is still right, and the other mod's whole
    contribution is gone. On the real pair that mutant drops Melina's two
    grafted states and her machine, and it is invisible to any test that only
    reads back what base already had.
    """
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    base_esd = esd.write(_esd({24: [(0, 2), (1, None), (2, None)]}))
    other_esd = esd.write(_esd({24: [(0, 3), (1, None), (2, None), (3, None)]}))
    base = _msgbnd_like({6: base_esd})
    out = merge.esd_three_way(base, _msgbnd_like({6: other_esd}),
                              _msgbnd_like({6: van}))

    shipped = _esd_entries(out)[6]
    assert shipped != base_esd
    merged = esd.read(shipped)
    group = next(g for g in merged.groups if g.id == 24)
    # State 3 is the other mod's, and only the graph merge puts it here.
    assert sorted(s.id for s in group.states) == [0, 1, 2, 3]


def test_esd_three_way_checks_the_bytes_it_is_about_to_ship():
    """Validating the graph handed to the writer is not validating the file.

    esdmerge.merge already checks the graph it returns, so a second check of
    that same object cannot fail and cannot be tested. Re-reading the written
    bytes can: it puts the writer itself inside the invariant, and picks up
    esd.read's own structural checks on real merged output, which production
    otherwise never exercises.
    """
    from ermlib.formats import esd
    from tests.test_esdmerge import _esd

    van = esd.write(_esd({24: [(0, 1), (1, None), (2, None)]}))
    base_esd = esd.write(_esd({24: [(0, 2), (1, None), (2, None)]}))
    other_esd = esd.write(_esd({24: [(0, 3), (1, None), (2, None), (3, None)]}))

    def corrupting_write(archive):
        """A writer that emits one state twice. The graph handed to it holds
        together; the file it produces does not."""
        group = archive.groups[0]
        return real_write(archive._replace(
            groups=(group._replace(states=group.states + group.states[1:2]),)))

    real_write = esd.write
    with mock.patch.object(esd, "write", corrupting_write):
        with pytest.raises(esdmerge.EsdMergeError):
            merge.esd_three_way(_msgbnd_like({6: base_esd}),
                                _msgbnd_like({6: other_esd}),
                                _msgbnd_like({6: van}))


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


def test_describe_note_renders_the_sentence_and_not_the_note():
    """The apply report prints whatever comes back from here, so anything that
    merely contains the numbers -- the note's own repr, say -- reads to a user
    as a stack trace fragment and tells them nothing. Compared against
    esdmerge.describe rather than a literal, so the prose lives in one place;
    what each sentence has to say is pinned next door in test_esdmerge."""
    note = esdmerge.Note(group=24, state=0, reason=esdmerge.Reason.BOTH_CHANGED)
    assert merge.describe_note(note) == esdmerge.describe(note)
    assert repr(note) not in merge.describe_note(note)


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


# --- params the reader refuses to parse ---


def _refuse_param(monkeypatch, blobs):
    """Make param.read refuse exactly these entry blobs.

    Three of vanilla's own entries really are unreadable until a tool re-saves
    them: FromSoft stores a strings offset past the end of the file. Refusing
    by blob rather than blanket-patching keeps the rest of the regulation (and
    the test's own reads) parseable."""
    from ermlib.formats import param
    real, targets = param.read, set(blobs)

    def reader(blob):
        if blob in targets:
            raise param.ParamError("PARAM offsets fall outside the file")
        return real(blob)
    monkeypatch.setattr(param, "read", reader)


def test_param_rows_takes_the_mods_table_whole_when_an_entry_will_not_parse(monkeypatch):
    """Base still matches vanilla, so the mod's table is the only edit there
    is and swapping the whole entry loses nothing. It is still a substitution
    the apply report has to name -- three entries went through this on the
    1.16-to-1.17 rebase without a word."""
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = van
    other = _regulation({1: (SP, {1: b"\x02" * 8}, 8)})
    _refuse_param(monkeypatch, [_entry(base, 1)])
    notes = []
    out = merge.param_rows(base, other, van, notes=notes)
    assert _entry(out, 1) == _entry(other, 1)
    assert notes == [merge.UnreadableParam(1)]


def test_param_rows_refuses_an_unparseable_entry_both_sides_changed(monkeypatch):
    """Neither side is vanilla, so there is a real edit on each and no way to
    read either -- picking one silently is the data loss this whole strategy
    exists to avoid."""
    van = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    base = _regulation({1: (SP, {1: b"\x02" * 8}, 8)})
    other = _regulation({1: (SP, {1: b"\x03" * 8}, 8)})
    _refuse_param(monkeypatch, [_entry(base, 1)])
    with pytest.raises(merge.MergeError, match="entry 1"):
        merge.param_rows(base, other, van)


def test_describe_note_renders_an_unreadable_param():
    text = merge.describe_note(merge.UnreadableParam(30))
    assert "30" in text and "mod" in text.lower()


# --- params that carry the same row id twice ---


def test_param_rows_refuses_a_duplicated_param_both_sides_changed():
    """An id is the only handle this merge has on a row, and in these params it
    doesn't name one. Keying rows by id would collapse the two copies last-wins
    and the shadowed edit would compare equal to vanilla -- dropped with the
    apply still reporting success."""
    van = _dup_regulation({1: (SP, [(1, bytes(8)), (5, b"\x50" * 8), (5, b"\x51" * 8)], 8)})
    base = _dup_regulation({1: (SP, [(1, b"\xaa" * 8), (5, b"\x50" * 8), (5, b"\x51" * 8)], 8)})
    other = _dup_regulation({1: (SP, [(1, bytes(8)), (5, b"\x99" * 8), (5, b"\x51" * 8)], 8)})
    with pytest.raises(merge.MergeError, match="more than once"):
        merge.param_rows(base, other, van)


def test_param_rows_keeps_the_base_when_a_duplicated_param_has_no_other_edit():
    """The common shape: every mod ships this table row-for-row as vanilla, but
    re-saved, so the entry blobs differ and the cheap skip can't fire. Nothing
    to merge, and the base's own edit must survive."""
    van = _dup_regulation({1: (SP, [(1, bytes(12)), (5, b"\x50" * 8 + bytes(4)),
                                    (5, b"\x51" * 8 + bytes(4))], 12)})
    base = _dup_regulation({1: (SP, [(1, b"\xaa" * 8 + bytes(4)), (5, b"\x50" * 8 + bytes(4)),
                                     (5, b"\x51" * 8 + bytes(4))], 12)})
    other = _dup_regulation({1: (SP, [(1, bytes(8)), (5, b"\x50" * 8), (5, b"\x51" * 8)], 8)})
    out = merge.param_rows(base, other, van)
    before = {e.id: e.data for e in regulation.entries(base)}
    assert {e.id: e.data for e in regulation.entries(out)}[1] == before[1]


def test_param_rows_takes_the_whole_duplicated_param_when_only_the_other_edited():
    """Base still matches vanilla row for row, so it has nothing to lose and
    the other side's table can be taken whole -- the one answer that needs no
    row lookup."""
    van = _dup_regulation({1: (SP, [(1, bytes(8)), (5, b"\x50" * 8), (5, b"\x51" * 8)], 8)})
    base = van
    other = _dup_regulation({1: (SP, [(1, bytes(8)), (5, b"\x99" * 8), (5, b"\x51" * 8)], 8)})
    out = merge.param_rows(base, other, van)
    assert ({e.id: e.data for e in regulation.entries(out)}[1]
            == {e.id: e.data for e in regulation.entries(other)}[1])


# --- how often a regulation gets decrypted ---


def _counting_unpack(monkeypatch):
    """Record every blob handed to regulation.unpack, and keep unpacking it."""
    seen = []
    real = regulation.unpack

    def spy(blob):
        seen.append(bytes(blob))
        return real(blob)

    monkeypatch.setattr(regulation, "unpack", spy)
    return seen


def test_param_rows_decrypts_each_regulation_only_once(monkeypatch):
    """AES over the real 2 MB regulation costs about five seconds a pass, and
    writing the result back used to re-decrypt the base the entry walk had
    already unpacked -- twelve wasted passes over a six-mod fold."""
    van = _regulation({1: (SP, {100: b"\x00" * 8}, 8)})
    base = _regulation({1: (SP, {100: b"\x00" * 8, 5: b"\x11" * 8}, 8)})
    other = _regulation({1: (SP, {100: b"\xff" * 8}, 8)})
    seen = _counting_unpack(monkeypatch)
    out = merge.param_rows(base, other, van)
    assert sorted(seen) == sorted(set(seen))
    rows = _rows(out, 1)
    assert rows[100] == b"\xff" * 8       # the other side's edit still lands
    assert rows[5] == b"\x11" * 8         # and the base's own row survives
