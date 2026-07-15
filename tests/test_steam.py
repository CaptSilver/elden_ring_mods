import os

import pytest

from ermlib.steam import read_appmanifest, cloud_saves


def _write_manifest(steam_root):
    sa = steam_root / "steamapps"
    sa.mkdir(parents=True)
    (sa / "appmanifest_1245620.acf").write_text(
        '"AppState"\n{\n"buildid" "0"\n"StateFlags" "1042"\n'
        '"SizeOnDisk" "0"\n"AutoUpdateBehavior" "1"\n}\n')


def test_appmanifest_reports_not_installed(tmp_path):
    _write_manifest(tmp_path)
    m = read_appmanifest(tmp_path)
    assert m["buildid"] == "0"
    assert m["installed"] is False       # SizeOnDisk 0


def test_appmanifest_reports_installed(tmp_path):
    sa = tmp_path / "steamapps"
    sa.mkdir(parents=True)
    (sa / "appmanifest_1245620.acf").write_text(
        '"AppState"\n{\n"buildid" "17516775"\n"StateFlags" "4"\n'
        '"SizeOnDisk" "27384950227"\n"AutoUpdateBehavior" "1"\n}\n')
    m = read_appmanifest(tmp_path)
    assert m["buildid"] == "17516775"
    assert m["installed"] is True        # nonzero SizeOnDisk and buildid


def test_cloud_saves_enumerates_accounts(tmp_path):
    (tmp_path / "steamapps").mkdir()
    ud = tmp_path / "userdata" / "65369667" / "1245620"
    ud.mkdir(parents=True)
    (ud / "remotecache.vdf").write_text(
        '"remotecache"\n{\n"EldenRing/76561198025635395/ER0000.sl2"\n'
        '{\n"size" "28967888"\n}\n}\n')
    saves = cloud_saves(tmp_path)
    assert len(saves) == 1
    assert saves[0]["account_id"] == "65369667"
    assert saves[0]["relpath"].endswith("ER0000.sl2")


def test_cloud_saves_survives_unreadable_userdata(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions; can't simulate EACCES")
    ud = tmp_path / "userdata"
    ud.mkdir()
    os.chmod(ud, 0o000)
    try:
        # iterdir() on the unreadable userdata dir raises PermissionError
        # internally; cloud_saves must swallow it and return [], never leak OSError.
        assert cloud_saves(tmp_path) == []
    finally:
        os.chmod(ud, 0o755)
