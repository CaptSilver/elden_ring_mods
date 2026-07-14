from ermlib.savefile import SaveFile


def test_reads_two_silverie_characters(real_save_bytes):
    sf = SaveFile.from_bytes(real_save_bytes)
    chars = sf.characters
    assert len(chars) == 2
    assert chars[0].name == "Silverie"
    assert chars[0].level == 294
    assert chars[1].level == 346
    assert 60000 < chars[0].seconds < 300000   # ~68h
    assert all(c.active for c in chars)
