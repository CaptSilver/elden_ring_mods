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
