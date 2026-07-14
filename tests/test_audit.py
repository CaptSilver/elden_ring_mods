from ermlib.savefile import SaveFile
from ermlib.audit import audit_save


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
