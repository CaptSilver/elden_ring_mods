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
    # refusal must fire before ANY filesystem mutation: save untouched, no backups dir
    assert save.exists()
    assert not (tmp_path / "backups").exists()


def test_quarantine_moves_save_and_reports_cloud(tmp_path):
    save = tmp_path / "ER0000.sl2"; save.write_bytes(b"x")
    rep = saves.quarantine(save, tmp_path / "backups",
                           cloud_saves=[{"account_id": "65369667",
                                         "relpath": "EldenRing/765../ER0000.sl2"}],
                           steam_up=False, stamp="20260714-1200")
    assert not save.exists()                                   # moved out of prefix
    # backup taken BEFORE the move — move-without-backup would be data loss
    assert list((tmp_path / "backups" / "quarantine-backup").glob("ER0000.sl2*"))
    assert any("cloud" in m.lower() for _, m in rep.items)     # cloud purge instruction


def test_listing_finds_the_quarantined_save(tmp_path):
    # quarantine() files the vanilla save two levels down, under
    # backups/quarantine/. A flat listing hides the one file the user most
    # needs to name when restoring.
    save = tmp_path / "ER0000.sl2"; save.write_bytes(b"x")
    bdir = tmp_path / "backups"
    saves.backup_save(save, bdir, label="orig", stamp="20260714-1200")
    saves.quarantine(save, bdir, cloud_saves=[], steam_up=False,
                     stamp="20260714-1200")

    listed = saves.list_backups(bdir)

    assert any(p.parent.name == "quarantine" for p in listed)
    assert any(p.parent == bdir for p in listed)
    assert listed == sorted(listed)


def test_listing_a_directory_that_was_never_created_is_empty(tmp_path):
    assert saves.list_backups(tmp_path / "nothing-here") == []
