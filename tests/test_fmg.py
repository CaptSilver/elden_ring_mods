import struct

import pytest

from ermlib.formats import fmg


def test_round_trip_preserves_entries():
    entries = {100: "Resurrect", 101: "Teleport", 102: None, 500: "Boss Resurrection"}
    assert fmg.read(fmg.write(entries)) == entries


def test_round_trip_preserves_non_ascii():
    """Game text is UTF-16 and full of accented and CJK characters. A latin-1
    assumption here silently mangles item names."""
    entries = {1: "Swordhand of Night Jolán", 2: "Rennala, Queen of the Full Moon"}
    assert fmg.read(fmg.write(entries)) == entries


def test_sparse_ids_survive_round_trip():
    """IDs are grouped into consecutive ranges on write. Gaps must not shift or
    drop entries when the ranges are rebuilt."""
    entries = {1: "a", 2: "b", 3: "c", 9000: "far", 9001: "apart", 70000: "z"}
    assert fmg.read(fmg.write(entries)) == entries


def test_empty_fmg_round_trips():
    assert fmg.read(fmg.write({})) == {}


def test_duplicate_strings_are_shared():
    """Identical strings share one blob offset. Vanilla files do this and it
    keeps the merged file from bloating."""
    entries = {i: "same text" for i in range(50)}
    blob = fmg.write(entries)
    assert fmg.read(blob) == entries
    assert blob.count("same text".encode("utf-16-le")) == 1


def test_read_rejects_an_unsupported_version():
    blob = bytearray(fmg.write({1: "x"}))
    blob[2] = 1
    with pytest.raises(fmg.FmgError):
        fmg.read(bytes(blob))


def test_read_rejects_range_pointing_past_offset_table():
    """A malformed FMG can have a range that claims more slots than exist in the
    offset table. This must be caught to prevent reading past the data."""
    blob = bytearray(fmg.write({1: "a", 2: "b"}))
    # Decrement string_count to make the range point past the table
    blob[0x10] = max(0, blob[0x10] - 1)
    with pytest.raises(fmg.FmgError, match="points past the offset table"):
        fmg.read(bytes(blob))


def test_read_rejects_a_reversed_range():
    """A corrupted range with first > last makes range(first, last + 1) empty,
    so the loop that fills `entries` contributes nothing for that range. A
    valid file corrupted this way used to come back as a short dict with no
    error at all -- this must raise instead."""
    blob = bytearray(fmg.write({1: "a", 2: "b"}))
    struct.pack_into("<iii", blob, fmg.HEADER_SIZE, 0, 5, 1)  # index, first, last
    with pytest.raises(fmg.FmgError, match="reversed"):
        fmg.read(bytes(blob))


def test_read_rejects_a_negative_string_offset():
    """`if string_off:` only checks truthiness, not sign. A negative offset is
    silently reinterpreted by Python as an index counted from the end of the
    buffer, so the wrong text comes back with no signal that anything went
    wrong -- verified: corrupting one offset to -10 turned {1: 'ab', 2: 'cd'}
    into {1: 'b', 2: 'cd'}."""
    blob = bytearray(fmg.write({1: "ab", 2: "cd"}))
    offsets_off, = struct.unpack_from("<q", blob, 0x18)
    struct.pack_into("<q", blob, offsets_off, -10)
    with pytest.raises(fmg.FmgError, match="negative"):
        fmg.read(bytes(blob))


def test_read_rejects_a_string_offset_past_the_end_of_the_buffer():
    """A string offset that lands past the end of the data must raise rather
    than let the null-terminator scan run off the buffer or wrap around."""
    blob = bytearray(fmg.write({1: "a"}))
    offsets_off, = struct.unpack_from("<q", blob, 0x18)
    struct.pack_into("<q", blob, offsets_off, len(blob) + 1000)
    with pytest.raises(fmg.FmgError):
        fmg.read(bytes(blob))


def test_read_rejects_a_corrupt_offsets_off_header_field():
    """FmgError exists so callers can catch one exception type for any corrupt
    mod file. A corrupted offsets_off that points past the buffer used to
    bubble up as a bare struct.error instead, breaking that contract with a
    raw traceback."""
    blob = bytearray(fmg.write({1: "a", 2: "b"}))
    struct.pack_into("<q", blob, 0x18, len(blob) + 1000)
    with pytest.raises(fmg.FmgError):
        fmg.read(bytes(blob))


def test_read_rejects_a_negative_range_count():
    """A negative range_count makes range(range_count) empty, which would
    silently return zero entries for a nonempty file instead of raising --
    the same failure shape as a reversed range, just at the header level."""
    blob = bytearray(fmg.write({1: "a", 2: "b"}))
    struct.pack_into("<i", blob, 0x0C, -1)
    with pytest.raises(fmg.FmgError, match="negative"):
        fmg.read(bytes(blob))


def test_read_rejects_a_range_count_that_overruns_the_range_table():
    """A range_count larger than the range table actually stored would run
    struct.unpack_from past the end of the buffer while parsing range headers,
    leaking a bare struct.error instead of FmgError."""
    blob = bytearray(fmg.write({1: "a", 2: "b"}))
    struct.pack_into("<i", blob, 0x0C, 1000)
    with pytest.raises(fmg.FmgError):
        fmg.read(bytes(blob))


def test_read_rejects_data_shorter_than_the_header():
    """A truncated download or a zero-byte file must raise a clear error
    instead of a struct.error from unpacking fields past the end of a buffer
    too small to hold even the header."""
    with pytest.raises(fmg.FmgError, match="shorter than its header"):
        fmg.read(b"\x00" * 10)


def test_read_rejects_an_unterminated_string():
    """If the buffer is truncated mid-string, the null-terminator scan in
    _read_utf16z must stop at the end of the buffer and raise instead of
    reading (or looping) past it."""
    blob = fmg.write({1: "a"})
    truncated = blob[:-2]  # drop the trailing null terminator of the one string
    with pytest.raises(fmg.FmgError, match="unterminated"):
        fmg.read(truncated)


def test_write_rejects_an_id_out_of_signed_32_bit_range():
    """Ids are packed as signed 32-bit integers. The public signature is just
    dict[int, str | None], so nothing stops a later merge step from handing
    write() an id that doesn't fit -- this must raise FmgError, not leak the
    bare struct.error from struct.pack_into."""
    with pytest.raises(fmg.FmgError):
        fmg.write({2**31: "x"})
