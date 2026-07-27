import struct

import pytest

from ermlib.formats import param
from ermlib.formats.param import ParamError

FORMAT_FLAGS = bytes.fromhex("00850700")


def make_param(ids, stride=8, param_type=b"TEST_PARAM_ST", fill=None):
    """Build a synthetic PARAM with the layout Elden Ring actually uses:
    header, 24-byte row table, fixed-stride row data, then one strings block."""
    n = len(ids)
    data_off = 0x40 + n * 24
    strings_off = data_off + n * stride
    strings = param_type + b"\x00\x00\x00\x00\x00"
    name_off = strings_off + len(param_type)      # the shared terminator

    header = bytearray(0x40)
    struct.pack_into("<I", header, 0x00, strings_off)
    struct.pack_into("<HHHH", header, 0x04, 0, 1, 4, n)
    struct.pack_into("<q", header, 0x10, strings_off)     # param_type_off
    header[0x2C:0x30] = FORMAT_FLAGS
    struct.pack_into("<q", header, 0x30, data_off)

    table = bytearray()
    for i, rid in enumerate(ids):
        table += struct.pack("<iIqq", rid, 0, data_off + i * stride, name_off)
    body = b"".join((fill(rid) if fill else bytes([rid & 0xFF]) * stride) for rid in ids)
    return bytes(header) + bytes(table) + body + strings


def test_read_exposes_ids_stride_and_row_data():
    blob = make_param([10, 20, 30])
    p = param.read(blob)
    assert [r.id for r in p.rows] == [10, 20, 30]
    assert p.stride == 8
    assert p.rows[1].data == bytes([20]) * 8
    assert p.param_type == b"TEST_PARAM_ST"


def test_round_trip_is_byte_identical():
    blob = make_param([1, 2, 3, 900])
    assert param.write(param.read(blob)) == blob


def test_round_trip_of_a_single_row_param():
    # The 11 single-row params have no offset gap to derive a stride from, so
    # they exercise the fallback path rather than the delta scan.
    blob = make_param([7], stride=64)
    p = param.read(blob)
    assert p.stride == 64
    assert param.write(p) == blob


def test_inserting_a_row_grows_the_file_by_stride_plus_table_entry():
    # The arithmetic oracle: one row costs its data plus its 24-byte table slot.
    blob = make_param([10, 30])
    p = param.read(blob)
    grown = param.replace_rows(p, list(p.rows) + [param.Row(20, bytes([20]) * 8, p.rows[0].name_off)])
    out = param.write(grown)
    assert len(out) == len(blob) + p.stride + 24


def test_replace_rows_keeps_ids_sorted_ascending():
    blob = make_param([10, 30])
    p = param.read(blob)
    grown = param.replace_rows(p, list(p.rows) + [param.Row(20, b"\x20" * 8, p.rows[0].name_off)])
    assert [r.id for r in param.read(param.write(grown)).rows] == [10, 20, 30]


def test_replace_rows_rewrites_offsets_so_the_result_reparses():
    blob = make_param([1, 2])
    p = param.read(blob)
    new = param.replace_rows(p, list(p.rows) + [param.Row(3, b"\xaa" * 8, p.rows[0].name_off)])
    reparsed = param.read(param.write(new))
    assert [r.id for r in reparsed.rows] == [1, 2, 3]
    assert reparsed.rows[2].data == b"\xaa" * 8
    assert reparsed.param_type == b"TEST_PARAM_ST"


def test_row_count_and_offsets_stay_consistent_after_a_write():
    blob = make_param([5, 6])
    p = param.read(blob)
    new = param.replace_rows(p, list(p.rows) + [param.Row(7, b"\x07" * 8, p.rows[0].name_off)])
    out = param.write(new)
    strings_off, = struct.unpack_from("<I", out, 0x00)
    row_count, = struct.unpack_from("<H", out, 0x0A)
    data_off, = struct.unpack_from("<q", out, 0x30)
    assert row_count == 3
    assert data_off == 0x40 + 3 * 24
    assert strings_off == data_off + 3 * 8


def test_rejects_rows_of_differing_length():
    blob = make_param([1, 2])
    p = param.read(blob)
    with pytest.raises(ParamError, match="stride"):
        param.write(param.replace_rows(p, [param.Row(1, b"short", p.rows[0].name_off)]))


def test_write_tolerates_the_duplicate_ids_real_params_ship():
    """RandomAppearParam ships 5,322 rows for 5,296 unique ids. Refusing to
    write duplicates would make that file unrepresentable; the ambiguity only
    matters when patching, which is where it's rejected."""
    blob = make_param([1, 2, 2, 3])
    assert param.write(param.read(blob)) == blob


def test_patch_refuses_to_insert_an_id_that_already_exists():
    p = param.read(make_param([1, 2]))
    with pytest.raises(ParamError, match="already exist"):
        param.patch_rows(p, insert={2: b"\x02" * 8})


def test_patch_refuses_to_overwrite_an_id_that_is_absent():
    # Guards the "base file is not what the patch was built against" case: if
    # the row we meant to replace isn't there, silently inserting it would
    # apply the patch to a file it was never validated against.
    p = param.read(make_param([1, 2]))
    with pytest.raises(ParamError, match="not in this param"):
        param.patch_rows(p, overwrite={99: b"\x99" * 8})


def test_patch_refuses_a_param_with_ambiguous_duplicate_ids():
    p = param.read(make_param([1, 2, 2]))
    with pytest.raises(ParamError, match="duplicate"):
        param.patch_rows(p, insert={5: b"\x05" * 8})


def test_patch_overwrites_and_inserts_in_one_pass():
    p = param.read(make_param([10, 30]))
    out = param.read(param.write(param.patch_rows(
        p, overwrite={10: b"\xee" * 8}, insert={20: b"\xdd" * 8})))
    assert out.row_ids() == [10, 20, 30]
    assert out.rows[0].data == b"\xee" * 8
    assert out.rows[1].data == b"\xdd" * 8
    assert out.rows[2].data == bytes([30]) * 8      # untouched


def test_patch_leaves_every_other_row_byte_identical():
    # The whole point of a narrow patch: nothing outside the named ids moves.
    p = param.read(make_param([1, 2, 3, 4, 5]))
    patched = param.read(param.write(param.patch_rows(p, insert={6: b"\x06" * 8})))
    before = {r.id: r.data for r in p.rows}
    after = {r.id: r.data for r in patched.rows}
    assert all(after[i] == before[i] for i in before)


def test_rejects_a_ragged_stride():
    """A param whose consecutive row offsets disagree isn't fixed-stride, and
    treating it as one would slice rows out of the middle of their neighbours."""
    blob = bytearray(make_param([1, 2, 3]))
    # push row 2's data offset 4 bytes late without moving anything else
    off_pos = 0x40 + 1 * 24 + 8
    cur, = struct.unpack_from("<q", blob, off_pos)
    struct.pack_into("<q", blob, off_pos, cur + 4)
    with pytest.raises(ParamError, match="stride"):
        param.read(bytes(blob))


def test_a_zero_name_offset_is_preserved_not_relocated():
    """0 means "this row has no name" and shipped params use it. Treating it as
    an address and adding the strings-block shift turns it into a pointer at
    whatever now sits at that offset."""
    blob = bytearray(make_param([1, 2]))
    struct.pack_into("<q", blob, 0x40 + 16, 0)          # row 1: no name
    p = param.read(bytes(blob))
    assert p.rows[0].name_off == 0
    out = param.read(param.write(param.patch_rows(p, insert={3: b"\x03" * 8})))
    assert out.rows[0].name_off == 0


def test_refuses_to_move_a_name_offset_that_sits_before_the_strings_block():
    # Such an offset belongs to a region whose displacement we never measured,
    # so relocating it on a row-count change would be a guess.
    blob = bytearray(make_param([1, 2]))
    struct.pack_into("<q", blob, 0x40 + 16, 8)
    p = param.read(bytes(blob))
    assert param.write(p) == bytes(blob)                # unchanged count is fine
    with pytest.raises(ParamError, match="before the strings block"):
        param.write(param.patch_rows(p, insert={3: b"\x03" * 8}))


def test_rejects_a_truncated_param():
    blob = make_param([1, 2, 3])
    with pytest.raises(ParamError):
        param.read(blob[:0x30])


# --- integration: real game data, skipped when it isn't on this machine ---

REGULATION_KEY = bytes.fromhex(
    "99BFFC366A6BC8C6F5827D093602D676C42892A01C207FB024D3AF4E493FEF99")


def _regulation_params(path):
    from ermlib.formats import aes, bnd4, dcx
    raw = path.read_bytes()
    return bnd4.read(dcx.read(aes.decrypt_cbc(REGULATION_KEY, raw[:16], raw[16:])))


@pytest.fixture(scope="module")
def clevers_regulation():
    from pathlib import Path
    p = Path("tools/me3/mods/clevers-moveset/regulation.bin")
    if not p.exists():
        pytest.skip("Clever's Moveset not installed — run `erm apply gameplay-extras`")
    return _regulation_params(p)


def test_every_readable_param_in_a_real_regulation_round_trips(clevers_regulation):
    """The cheapest guard on the offset-reflow logic. A param that reads but
    writes back differently means the header re-stamp or the strings shift is
    wrong, and nothing else in the suite would notice."""
    checked, failed = [], []
    for entry in clevers_regulation:
        try:
            parsed = param.read(entry.data)
        except ParamError:
            continue            # a layout this reader refuses is not a round-trip claim
        checked.append(entry.id)
        if param.write(parsed) != entry.data:
            failed.append(entry.id)
    assert checked, "no params were readable at all — the container layer is broken"
    assert not failed, f"params did not round-trip byte-identically: {failed}"


def test_patching_speffect_rows_leaves_every_other_row_untouched(clevers_regulation):
    """The invariant the whole narrow-patch design exists to protect: inserting
    rows must not disturb a single byte of anything else, and in particular must
    not revert an authored row back to vanilla."""
    speffect = next(e for e in clevers_regulation
                    if param.read(e.data).param_type == b"SP_EFFECT_PARAM_ST")
    base = param.read(speffect.data)
    blob = b"\xa5" * base.stride
    new_ids = [i for i in (990001, 990002) if i not in set(base.row_ids())]
    patched = param.read(param.write(param.patch_rows(
        base, insert={i: blob for i in new_ids})))

    before = {r.id: r.data for r in base.rows}
    after = {r.id: r.data for r in patched.rows}
    assert len(patched.rows) == len(base.rows) + len(new_ids)
    assert all(after[i] == before[i] for i in before)
    assert all(after[i] == blob for i in new_ids)
    assert patched.row_ids() == sorted(patched.row_ids())


def test_a_row_insert_costs_exactly_its_data_plus_its_table_entry(clevers_regulation):
    speffect = next(e for e in clevers_regulation
                    if param.read(e.data).param_type == b"SP_EFFECT_PARAM_ST")
    base = param.read(speffect.data)
    ids = [i for i in (990001, 990002) if i not in set(base.row_ids())]
    out = param.write(param.patch_rows(base, insert={i: b"\x00" * base.stride for i in ids}))
    assert len(out) - len(speffect.data) == len(ids) * (base.stride + 24)
