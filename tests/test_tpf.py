import struct

import pytest

from ermlib.formats import tpf


def _build(textures, platform=0, flag2=3, encoding=1):
    """Hand-assemble a TPF the way the shipped files are laid out: header, entry
    table, name block, then texture data — every section packed, no padding."""
    count = len(textures)
    table_end = tpf.HEADER_SIZE + count * tpf.ENTRY_SIZE
    names, name_off = b"", table_end
    offsets = []
    for name, _data, _fmt in textures:
        offsets.append(name_off)
        encoded = name.encode("utf-16-le") + b"\0\0"
        names += encoded
        name_off += len(encoded)
    pad = -len(names) % tpf.NAME_ALIGN
    data_start = table_end + len(names) + pad
    body, cursor, table = b"", data_start, b""
    for (name, data, fmt), noff in zip(textures, offsets):
        table += struct.pack("<IIBBBBII", cursor, len(data), fmt, 0, 1, 0, noff, 0)
        body += data
        cursor += len(data)
    header = struct.pack("<4sIIBBBB", tpf.MAGIC, pad + len(body), count,
                         platform, flag2, encoding, 0)
    return header + table + names + b"\0" * pad + body


def test_read_returns_every_texture_with_its_name_and_bytes():
    raw = _build([("SB_Icon_02_A", b"AAAA", 102), ("SB_Status_GRO_00", b"BB", 102)])
    archive = tpf.read(raw)
    assert [t.name for t in archive.textures] == ["SB_Icon_02_A", "SB_Status_GRO_00"]
    assert [t.data for t in archive.textures] == [b"AAAA", b"BB"]
    assert archive.encoding == 1


def test_write_round_trips_byte_identically():
    """The writer has to be provably faithful before it's allowed near a 206 MB
    shipped asset — the same bar bnd4.rebuild is held to."""
    raw = _build([("A", b"\x01\x02\x03\x04", 0), ("Bee", b"\xff" * 32, 102),
                  ("Cee_long_name", b"", 1)])
    assert tpf.write(tpf.read(raw)) == raw


def test_write_recomputes_offsets_after_a_texture_is_appended():
    """Adding an entry grows the table and the name block, so every data offset
    downstream of it moves. A writer that kept the old offsets would hand the
    game garbage for every texture."""
    raw = _build([("First", b"1" * 16, 0)])
    archive = tpf.read(raw)
    grown = archive.with_textures(
        archive.textures + (tpf.Texture("Added", b"2" * 8, 102, 0, 1, 0),))
    out = tpf.read(tpf.write(grown))
    assert [t.name for t in out.textures] == ["First", "Added"]
    assert [t.data for t in out.textures] == [b"1" * 16, b"2" * 8]
    # Offsets are internally consistent: contiguous, in table order, starting
    # after the name block.
    raw_out = tpf.write(grown)
    data_size, count = struct.unpack_from("<II", raw_out, 4)
    assert count == 2
    assert data_size == 24


def test_write_pads_the_name_block_and_counts_the_pad_in_data_size():
    """Both shipped menu TPFs agree: the name block is padded to a 4-byte
    boundary, and dataSize spans everything after the names — so it includes the
    padding, not just the sum of texture sizes."""
    # A single 3-character name: 3*2 + 2 terminator = 8 bytes, already aligned.
    aligned = tpf.write(tpf.read(_build([("Abc", b"XY", 0)])))
    assert struct.unpack_from("<I", aligned, 4)[0] == 2
    # Two characters: 2*2 + 2 = 6 bytes, so 2 bytes of padding follow.
    padded = tpf.write(tpf.read(_build([("Ab", b"XY", 0)])))
    assert struct.unpack_from("<I", padded, 4)[0] == 4
    data_off = struct.unpack_from("<I", padded, tpf.HEADER_SIZE)[0]
    assert data_off % tpf.NAME_ALIGN == 0
    assert padded[data_off:data_off + 2] == b"XY"


def test_read_rejects_a_file_that_is_not_a_tpf():
    with pytest.raises(tpf.TpfError):
        tpf.read(b"BND4" + b"\0" * 64)


def test_read_rejects_a_truncated_header():
    with pytest.raises(tpf.TpfError):
        tpf.read(b"TPF\0" + b"\0" * 4)


def test_read_rejects_a_texture_whose_data_runs_past_the_file():
    raw = bytearray(_build([("A", b"1" * 16, 0)]))
    struct.pack_into("<I", raw, tpf.HEADER_SIZE + 4, 1 << 30)   # fileSize
    with pytest.raises(tpf.TpfError):
        tpf.read(bytes(raw))


def test_read_rejects_a_count_the_file_cannot_hold():
    """A bogus count from a third-party archive must raise here rather than let
    the entry loop walk off the end of the buffer."""
    raw = bytearray(_build([("A", b"1" * 16, 0)]))
    struct.pack_into("<I", raw, 8, 1 << 20)
    with pytest.raises(tpf.TpfError):
        tpf.read(bytes(raw))


def test_a_float_struct_entry_is_refused_rather_than_misparsed():
    """FloatStruct makes the entry stride variable. No shipped menu TPF uses it,
    and silently reading the next entry at the wrong offset would corrupt every
    texture after it — so refuse instead of guessing."""
    raw = bytearray(_build([("A", b"1" * 16, 0)]))
    struct.pack_into("<I", raw, tpf.HEADER_SIZE + 16, 1)        # hasFloatStruct
    with pytest.raises(tpf.TpfError):
        tpf.read(bytes(raw))
