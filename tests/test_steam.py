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
