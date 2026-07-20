import struct
from pathlib import Path

import pytest

from ermlib.formats import bnd4, dcx

CLEVERS_MENU = Path("tools/me3/mods/clevers-moveset/msg/engus/menu_dlc02.msgbnd.dcx")


def _synthetic_bnd4(entries, hash_table=b"\xAB\xCD\xEF\x01" * 4):
    """Build a minimal BND4 for tests: header, entry headers, names, a stand-in
    hash table, then padded data. Mirrors the real layout closely enough to
    prove the structural clone preserves everything it should."""
    count = len(entries)
    entry_header_size = 0x24
    names_off = 0x40 + count * entry_header_size
    name_blob, name_offs = bytearray(), []
    for _id, name, _data in entries:
        name_offs.append(names_off + len(name_blob))
        name_blob += name.encode("utf-16-le") + b"\x00\x00"
    hash_off = names_off + len(name_blob)
    data_off = (hash_off + len(hash_table) + 0xF) & ~0xF

    out = bytearray(0x40)
    out[0:4] = b"BND4"
    struct.pack_into("<i", out, 8, 0x00010000)
    struct.pack_into("<i", out, 0x0C, count)
    struct.pack_into("<q", out, 0x10, 0x40)
    out[0x18:0x20] = b"07D7R6\x00\x00"
    struct.pack_into("<q", out, 0x20, entry_header_size)
    struct.pack_into("<q", out, 0x28, data_off)
    out[0x30], out[0x31], out[0x32], out[0x33] = 1, 0x74, 4, 0
    struct.pack_into("<q", out, 0x38, hash_off)

    headers, blob, cursor = bytearray(), bytearray(), data_off
    for (eid, _name, data), noff in zip(entries, name_offs):
        e = bytearray(entry_header_size)
        struct.pack_into("<i", e, 0, 0x40)
        struct.pack_into("<i", e, 4, -1)
        struct.pack_into("<q", e, 8, len(data))
        struct.pack_into("<q", e, 0x10, len(data))
        struct.pack_into("<I", e, 0x18, cursor)
        struct.pack_into("<i", e, 0x1C, eid)
        struct.pack_into("<i", e, 0x20, noff)
        headers += e
        blob += data
        cursor += len(data)
        pad = (-len(data)) % 0x10
        blob += b"\x00" * pad
        cursor += pad

    middle = bytearray(name_blob)
    middle += hash_table
    middle += b"\x00" * (data_off - (hash_off + len(hash_table)))
    return bytes(out + headers + middle + blob)


def test_read_returns_entries_in_order():
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA"), (20, "b.fmg", b"BBBB")])
    entries = bnd4.read(raw)
    assert [(e.id, e.name, e.data) for e in entries] == [
        (10, "a.fmg", b"AAA"), (20, "b.fmg", b"BBBB")]


def test_rebuild_with_no_changes_is_byte_identical():
    """The strongest guarantee this module offers: an identity rebuild must
    reproduce the input exactly, hash table and all."""
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA"), (20, "b.fmg", b"BBBB")])
    assert bnd4.rebuild(raw, {}) == raw


def test_rebuild_preserves_the_hash_table():
    """Vanilla populates a hash-bucket table at 0x38. Zeroing it passes every
    test that re-reads by id and then faults inside the game's loader, so assert
    the bytes survive a rebuild that changes entry sizes."""
    marker = b"\xDE\xAD\xBE\xEF" * 4
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA")], hash_table=marker)
    rebuilt = bnd4.rebuild(raw, {10: b"much longer replacement data"})
    hash_off, = struct.unpack_from("<q", raw, 0x38)
    assert rebuilt[hash_off:hash_off + len(marker)] == marker


def test_rebuild_replaces_entry_data():
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA"), (20, "b.fmg", b"BBBB")])
    rebuilt = bnd4.rebuild(raw, {20: b"replaced with something longer"})
    by_id = {e.id: e.data for e in bnd4.read(rebuilt)}
    assert by_id == {10: b"AAA", 20: b"replaced with something longer"}


def test_rebuild_keeps_the_header_region_identical():
    """Everything before the data section is cloned, so name offsets and the
    hash table stay valid without recomputing any of them."""
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA")])
    rebuilt = bnd4.rebuild(raw, {10: b"different length entirely"})
    data_off, = struct.unpack_from("<q", raw, 0x28)
    # Entry headers hold sizes and offsets that legitimately change; everything
    # from the name blob onward must not.
    names_off = 0x40 + 1 * 0x24
    assert rebuilt[names_off:data_off] == raw[names_off:data_off]


def test_rebuild_rejects_an_unknown_entry_id():
    """Replacing an id the archive doesn't hold means the caller's assumptions
    are wrong. Silently ignoring it would drop the merge on the floor."""
    raw = _synthetic_bnd4([(10, "a.fmg", b"AAA")])
    with pytest.raises(bnd4.Bnd4Error):
        bnd4.rebuild(raw, {999: b"nope"})


def test_read_rejects_a_non_bnd4():
    with pytest.raises(bnd4.Bnd4Error):
        bnd4.read(b"NOTBND4" + b"\x00" * 128)


# --- Hardening: BND4 archives are arbitrary third-party mod files. Every guard
# below constructs the specific malformed input it defends against, and would
# fail (wrong exception type, wrong data, or a hang) if the guard were removed.


def test_read_rejects_a_negative_entry_count():
    """A negative count makes range(count) empty, which would silently return
    zero entries for a nonempty file instead of raising."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<i", raw, 0x0C, -1)
    with pytest.raises(bnd4.Bnd4Error, match="negative"):
        bnd4.read(bytes(raw))


def test_read_rejects_a_zero_entry_header_size_when_entries_exist():
    """A header size of 0 with count > 0 would let range(count) still land on
    a fixed header table size, so an absurd count (e.g. 10**9) would sail past
    the table-fits-the-file check and hang the read loop instead of raising."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<i", raw, 0x0C, 10**9)
    struct.pack_into("<q", raw, 0x20, 0)
    with pytest.raises(bnd4.Bnd4Error):
        bnd4.read(bytes(raw))


def test_read_rejects_an_entry_header_table_that_overruns_the_file():
    """A valid header size times an absurd count must be caught as not fitting
    the file, rather than iterating and reading past the buffer."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<i", raw, 0x0C, 10**6)
    with pytest.raises(bnd4.Bnd4Error, match="doesn't fit"):
        bnd4.read(bytes(raw))


def test_read_rejects_a_data_offset_that_overlaps_the_entry_headers():
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<q", raw, 0x28, 0)  # inside the header table
    with pytest.raises(bnd4.Bnd4Error, match="overlaps"):
        bnd4.read(bytes(raw))


def test_read_rejects_a_data_offset_that_exceeds_the_file():
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<q", raw, 0x28, len(raw) + 1000)
    with pytest.raises(bnd4.Bnd4Error, match="exceeds"):
        bnd4.read(bytes(raw))


def test_read_rejects_an_entry_header_size_smaller_than_its_fields():
    """A header size smaller than the fixed fields read per entry can let the
    header table appear to 'fit' by count*header_size, while the actual field
    reads for later entries still fall outside their own header, or even
    outside the buffer. Must be caught before the per-entry loop runs, not
    discovered by a bare struct.error once it does."""
    data = bytearray(0x50)
    data[0:4] = b"BND4"
    struct.pack_into("<i", data, 0x0C, 1)       # count = 1
    struct.pack_into("<q", data, 0x20, 4)       # header_size = 4, too small for 0x24 of fields
    struct.pack_into("<q", data, 0x28, 0x48)    # data_offset within [headers_end, len(data)]
    with pytest.raises(bnd4.Bnd4Error, match="smaller than"):
        bnd4.read(bytes(data))


def test_read_rejects_a_negative_entry_size():
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<q", raw, 0x40 + 0x10, -1)
    with pytest.raises(bnd4.Bnd4Error, match="negative"):
        bnd4.read(bytes(raw))


def test_read_rejects_entry_data_that_runs_past_the_buffer():
    """Python slicing doesn't raise on an out-of-range stop index, so without
    this check a corrupted size/offset would hand back truncated data instead
    of failing."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<q", raw, 0x40 + 0x10, len(raw) + 1000)
    with pytest.raises(bnd4.Bnd4Error):
        bnd4.read(bytes(raw))


def test_read_rejects_a_negative_name_offset():
    """A negative name offset is silently reinterpreted as an index counted
    from the end of the buffer, reading the wrong (or coincidentally
    null-terminated) bytes instead of raising."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<i", raw, 0x40 + 0x20, -10)
    with pytest.raises(bnd4.Bnd4Error, match="name offset"):
        bnd4.read(bytes(raw))


def test_read_rejects_an_unterminated_entry_name():
    """If the buffer ends mid-name, the null-terminator scan in _read_utf16z
    must stop at the end of the buffer and raise instead of reading past it."""
    data = bytearray(0x66)
    data[0:4] = b"BND4"
    struct.pack_into("<i", data, 0x0C, 1)        # count = 1
    struct.pack_into("<q", data, 0x20, 0x24)     # header_size = 0x24
    struct.pack_into("<q", data, 0x28, 0x64)     # data_offset: right after the header table
    struct.pack_into("<q", data, 0x40 + 0x10, 0)     # size = 0
    struct.pack_into("<I", data, 0x40 + 0x18, 0x64)  # offset = data_offset
    struct.pack_into("<i", data, 0x40 + 0x1C, 1)     # id = 1
    struct.pack_into("<i", data, 0x40 + 0x20, 0x64)  # name_offset: last two bytes of the buffer
    data[0x64:0x66] = "A".encode("utf-16-le")    # one char, no null terminator, buffer ends here
    with pytest.raises(bnd4.Bnd4Error, match="unterminated"):
        bnd4.read(bytes(data))


def test_read_rejects_a_name_offset_past_the_end_of_the_buffer():
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    struct.pack_into("<i", raw, 0x40 + 0x20, len(raw) + 1000)
    with pytest.raises(bnd4.Bnd4Error, match="name offset"):
        bnd4.read(bytes(raw))


def test_rebuild_preserves_an_unaligned_leading_gap_before_the_first_entry():
    """Real archives can leave a few bytes between the declared data section
    and where the first entry actually starts (data_offset isn't always
    itself 16-byte aligned) -- discovered via the real-file identity check
    below, which the aligned-by-construction fixture can't reproduce on its
    own. Anchor on the first entry's own recorded offset and prove those
    bytes survive a rebuild that touches another entry."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA"), (20, "b.fmg", b"BBBB")]))
    data_offset, = struct.unpack_from("<q", raw, 0x28)
    gap = b"\x11" * 8  # non-zero, so an accidental zero-fill wouldn't pass silently
    raw = raw[:data_offset] + gap + raw[data_offset:]
    for i in range(2):
        base = 0x40 + i * 0x24
        off, = struct.unpack_from("<I", raw, base + 0x18)
        struct.pack_into("<I", raw, base + 0x18, off + len(gap))
    raw = bytes(raw)

    rebuilt = bnd4.rebuild(raw, {20: b"replaced with something longer"})
    by_id = {e.id: e.data for e in bnd4.read(rebuilt)}
    assert by_id == {10: b"AAA", 20: b"replaced with something longer"}
    first_off, = struct.unpack_from("<I", rebuilt, 0x40 + 0x18)
    assert rebuilt[data_offset:first_off] == gap


def test_rebuild_rejects_a_first_entry_offset_before_the_data_section():
    """If the first entry's own recorded offset points before the declared
    data section, slicing the (negative-length) gap would silently return an
    empty byte string instead of raising -- this must be caught explicitly."""
    raw = bytearray(_synthetic_bnd4([(10, "a.fmg", b"AAA")]))
    data_offset, = struct.unpack_from("<q", raw, 0x28)
    struct.pack_into("<I", raw, 0x40 + 0x18, data_offset - 4)
    with pytest.raises(bnd4.Bnd4Error, match="precedes"):
        bnd4.rebuild(bytes(raw), {10: b"different length entirely"})


@pytest.mark.skipif(not CLEVERS_MENU.exists(), reason="Clever's Moveset not installed")
def test_identity_rebuild_of_a_real_msgbnd():
    """A real archive exercises the populated hash table and the true entry
    layout, neither of which the synthetic fixture fully reproduces."""
    raw = dcx.read(CLEVERS_MENU.read_bytes())
    assert bnd4.rebuild(raw, {}) == raw
