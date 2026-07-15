from pathlib import Path
import pytest
from ermlib import saves
from ermlib.errors import SafetyError


def test_backup_copies_with_label(tmp_path):
    save = tmp_path / "ER0000.co2"; save.write_bytes(b"hello")
    bdir = tmp_path / "backups"
    out = saves.backup_save(save, bdir, label="preboss", stamp="20260714-1200")
    assert out.exists() and out.read_bytes() == b"hello"
    assert "preboss" in out.name


def test_quarantine_refuses_while_steam_running(tmp_path):
    save = tmp_path / "ER0000.sl2"; save.write_bytes(b"x")
    with pytest.raises(SafetyError):
        saves.quarantine(save, tmp_path / "backups", cloud_saves=[], steam_up=True,
                         stamp="20260714-1200")


def test_quarantine_moves_save_and_reports_cloud(tmp_path):
    save = tmp_path / "ER0000.sl2"; save.write_bytes(b"x")
    rep = saves.quarantine(save, tmp_path / "backups",
                           cloud_saves=[{"account_id": "65369667",
                                         "relpath": "EldenRing/765../ER0000.sl2"}],
                           steam_up=False, stamp="20260714-1200")
    assert not save.exists()                                   # moved out of prefix
    assert any("cloud" in m.lower() for _, m in rep.items)     # cloud purge instruction
