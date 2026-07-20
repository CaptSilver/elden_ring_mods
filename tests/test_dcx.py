import zlib

import pytest

from ermlib.formats import dcx


def test_dflt_round_trip():
    payload = b"BND4" + bytes(range(256)) * 40
    blob = dcx.write_dflt(payload)
    assert blob[:4] == b"DCX\x00"
    assert blob[0x28:0x2c] == b"DFLT"
    assert dcx.read(blob) == payload


def test_write_rejects_level_zero():
    """The game's DCX reader validates the level byte as 1-9 (dec al; cmp al,8;
    ja fail). zlib level 0 writes a 0 there and the file is rejected at load."""
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=0)


def test_write_rejects_level_above_nine():
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=10)


def test_header_fields_match_a_known_good_layout():
    """Byte-for-byte agreement with the DFLT headers Clever's Moveset ships,
    which the game loads today. Sizes are the only fields allowed to differ."""
    payload = b"x" * 1000
    blob = dcx.write_dflt(payload)
    assert blob[0x00:0x04] == b"DCX\x00"
    assert blob[0x18:0x1c] == b"DCS\x00"
    assert blob[0x24:0x28] == b"DCP\x00"
    assert blob[0x2c:0x30] == (0x20).to_bytes(4, "big")
    assert blob[0x30] == 9
    assert int.from_bytes(blob[0x04:0x08], "big") <= 0x11000
    assert int.from_bytes(blob[0x1c:0x20], "big") == len(payload)


def test_read_rejects_a_non_dcx():
    with pytest.raises(dcx.DcxError):
        dcx.read(b"NOTDCX" + b"\x00" * 200)


def test_read_rejects_an_unknown_compression():
    blob = bytearray(dcx.write_dflt(b"payload"))
    blob[0x28:0x2c] = b"ZSTD"
    with pytest.raises(dcx.DcxError):
        dcx.read(bytes(blob))
