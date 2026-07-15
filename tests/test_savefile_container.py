import pytest
from ermlib.savefile import SaveFile, NotAnEldenRingSave


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
