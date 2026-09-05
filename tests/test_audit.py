from ermlib.savefile import SaveFile
from ermlib.audit import audit_save

from tests.test_savefile_container import build_bnd4


def test_real_save_has_no_decisive_tampering(real_save_bytes):
    sf = SaveFile.from_bytes(real_save_bytes)
    res = audit_save(sf)
    assert res.tampered is False
    assert res.decisive == []
    # honesty: a clean result is never a certificate
    assert "cannot" in res.caveat.lower()


def test_stat_over_99_is_decisive(real_save_bytes, monkeypatch):
    sf = SaveFile.from_bytes(real_save_bytes)
    orig = sf.slot_data

    def patched(slot, name):
        sd = orig(slot, name)
        sd.stats["vigor"] = 120  # impossible
        return sd

    monkeypatch.setattr(sf, "slot_data", patched)
    res = audit_save(sf)
    assert res.tampered is True
    assert any("vigor" in f.message and f.severity == "decisive" for f in res.findings)


def test_md5_findings_survive_an_unreadable_character_list():
    # A truncated save keeps its header and name table, so every entry parses
    # and the MD5 mismatch — the whole point of the audit — is detectable.
    # Walking the characters must not throw that diagnosis away.
    data = build_bnd4([b"body"] * 10 + [b"short", b"reg"], break_md5={3})
    res = audit_save(SaveFile.from_bytes(data))
    assert any("USER_DATA003" in f.message and "MD5" in f.message
               for f in res.decisive)
    assert any("character" in f.message for f in res.decisive)
