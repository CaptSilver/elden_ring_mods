import hashlib
import struct

import pytest
from ermlib.savefile import SaveFile, NotAnEldenRingSave


def build_bnd4(bodies, break_md5=()):
    """Assemble a well-formed BND4 container around `bodies`.

    Shared with the audit tests: the point of these cases is that the container
    parses cleanly (header, 0x20-stride entry table, valid names, valid
    per-entry MD5) and only the save-shaped assumptions on top of it are wrong.
    `break_md5` names entry indexes whose stored digest should not match.
    """
    count = len(bodies)
    names_off = 0x40 + count * 0x20
    names, name_offs = b"", []
    for i in range(count):
        name_offs.append(names_off + len(names))
        names += f"USER_DATA{i:03d}".encode("utf-16-le") + b"\x00\x00"
    data_off = names_off + len(names)
    table, blobs = b"", b""
    for i, body in enumerate(bodies):
        digest = hashlib.md5(body).digest()
        if i in break_md5:
            digest = bytes(b ^ 0xFF for b in digest)
        blob = digest + body
        table += (b"\x00" * 8 + struct.pack("<q", len(blob))
                  + struct.pack("<I", data_off + len(blobs))
                  + struct.pack("<I", name_offs[i]) + b"\x00" * 8)
        blobs += blob
    header = b"BND4" + b"\x00" * 8 + struct.pack("<I", count)
    return header.ljust(0x40, b"\x00") + table + names + blobs




def test_parses_twelve_entries_and_all_md5_validate(real_save_bytes):
    sf = SaveFile.from_bytes(real_save_bytes)
    assert len(sf.entries) == 12
    assert [e.name for e in sf.entries] == [f"USER_DATA{i:03d}" for i in range(12)]
    # the game's own per-entry MD5 is our proof the parse is correct
    assert sf.all_md5_ok is True
    assert len(sf.slots) == 10
    assert sf.profile_entry.index == 10
    assert sf.regulation_entry.index == 11


def test_rejects_playstation_save():
    ps = b"\xcb\x01\x9c\x2c" + b"\x00" * 128
    with pytest.raises(NotAnEldenRingSave):
        SaveFile.from_bytes(ps)


def test_rejects_non_save():
    with pytest.raises(NotAnEldenRingSave):
        SaveFile.from_bytes(b"not a save at all")


def test_rejects_truncated_bnd4_cleanly():
    # Tagged BND4 but far too short for a real entry table — the parse must
    # raise our own error, not leak a bare struct.error/IndexError/ValueError
    # up through cmd_audit as a raw traceback.
    truncated = b"BND4" + b"\x00" * 8
    with pytest.raises(NotAnEldenRingSave):
        SaveFile.from_bytes(truncated)


def test_rejects_a_bnd4_that_is_not_a_full_save():
    # Parses as a container but has nowhere near a save's 12 entries, so the
    # profile/regulation lookups would run off the end of the list.
    with pytest.raises(NotAnEldenRingSave):
        SaveFile.from_bytes(build_bnd4([b"body"] * 2))


def test_rejects_a_truncated_profile_entry():
    sf = SaveFile.from_bytes(build_bnd4([b"body"] * 11 + [b"short"]))
    with pytest.raises(NotAnEldenRingSave):
        sf.characters
