"""The shared Seamless Co-op archive has to keep matching what production reads."""
import os
import zipfile
from pathlib import Path

from ermlib import install, me3profile
from tests.ersc_fixtures import make_ersc_zip


def test_ersc_fixture_carries_the_members_production_reaches_for(tmp_path, tmp_game):
    # apply_ersc rewrites the settings file by hardcoded path, and me3profile
    # points me3 at the native by hardcoded path. Both are relative to the
    # archive's own layout, so a fixture that drifted from the real release
    # would make every test built on it pass against a layout that can't exist.
    z = tmp_path / "ersc.zip"
    make_ersc_zip(z)

    install.apply_ersc(z, tmp_game, password="hunter2")
    settings = [p for p in (tmp_game / "SeamlessCoop").rglob("*") if p.suffix == ".ini"]
    assert settings and "cooppassword = hunter2" in settings[0].read_text()

    me3_dir = tmp_path / "me3"
    me3profile.reconcile({"seamless-coop": {}}, me3_dir, tmp_game)
    natives = [line.split("=", 1)[1].strip().strip("'\"")
               for line in (me3_dir / "erm-coop.me3").read_text().splitlines()
               if line.startswith("path = ")]
    assert natives
    members = zipfile.ZipFile(z).namelist()
    for native in natives:
        assert os.path.relpath(native, Path(tmp_game).resolve()) in members
