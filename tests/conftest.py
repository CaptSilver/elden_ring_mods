import pathlib
import shutil
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
REAL_SAVE = REPO / "backups" / "pre-quarantine" / "ER0000.sl2.2025-02-15"


@pytest.fixture(scope="session")
def real_save_bytes():
    if not REAL_SAVE.exists():
        pytest.skip(f"fixture save missing at {REAL_SAVE}")
    return REAL_SAVE.read_bytes()


@pytest.fixture
def tmp_game(tmp_path):
    """A throwaway fake Game/ dir for install/doctor tests."""
    g = tmp_path / "Game"
    g.mkdir()
    (g / "eldenring.exe").write_bytes(b"\x00")
    (g / "start_protected_game.exe").write_bytes(b"\x00" * 16)
    return g
