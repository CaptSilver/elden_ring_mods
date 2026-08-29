import struct
import pytest

from ermlib import gamebuild
from ermlib.gamebuild import GameBuildError


def _fake_exe(major, minor, patch, build, prefix=b"MZ" + b"\x00" * 64):
    """A blob carrying one VS_FIXEDFILEINFO, laid out as a real PE does.

    Only the signature and the two version dwords matter to the reader, so the
    surrounding bytes are filler -- the point is to prove we find the struct by
    signature rather than by walking resource directories.
    """
    ms = (major << 16) | minor
    ls = (patch << 16) | build
    return (prefix
            + struct.pack("<I", 0xFEEF04BD)      # signature
            + struct.pack("<I", 0x00010000)      # struct version
            + struct.pack("<II", ms, ls)         # dwFileVersionMS / LS
            + b"\x00" * 32)


def test_reads_the_real_game_exe_version(tmp_path):
    p = tmp_path / "eldenring.exe"
    p.write_bytes(_fake_exe(2, 7, 0, 0))
    assert gamebuild.read_exe_version(p) == "2.7.0.0"


def test_reads_the_stale_swapped_launcher_version(tmp_path):
    # The exact skew found on this machine: the hardened swap was a July copy.
    p = tmp_path / "start_protected_game.exe"
    p.write_bytes(_fake_exe(2, 6, 2, 0))
    assert gamebuild.read_exe_version(p) == "2.6.2.0"


def test_exe_without_a_version_resource_raises(tmp_path):
    p = tmp_path / "eldenring.exe"
    p.write_bytes(b"MZ" + b"\x00" * 500)
    with pytest.raises(GameBuildError, match="no version resource"):
        gamebuild.read_exe_version(p)


def test_truncated_version_resource_raises(tmp_path):
    p = tmp_path / "eldenring.exe"
    p.write_bytes(b"MZ" + struct.pack("<I", 0xFEEF04BD) + b"\x00" * 4)
    with pytest.raises(GameBuildError, match="truncated"):
        gamebuild.read_exe_version(p)


def test_missing_exe_raises_rather_than_returning_none(tmp_path):
    with pytest.raises(GameBuildError):
        gamebuild.read_exe_version(tmp_path / "nope.exe")


@pytest.mark.parametrize("regver,app", [
    ("11701000", "1.17.0"),
    ("11611000", "1.16.1"),
    ("11601000", "1.16.0"),
])
def test_app_version_maps_every_observed_regulation_version(regver, app):
    assert gamebuild.app_version(regver) == app


@pytest.mark.parametrize("bad", ["", "1170100", "117010000", "abcdefgh", "11702000"])
def test_app_version_refuses_anything_it_has_not_seen(bad):
    # Refusing an unrecognised shape matters more than parsing it: a wrong app
    # version would be reported to the user as fact.
    with pytest.raises(GameBuildError):
        gamebuild.app_version(bad)


def test_regulation_version_is_read_from_offset_0x18(monkeypatch):
    payload = bytearray(b"BND4" + b"\x00" * 0x40)
    payload[0x18:0x20] = b"11701000"
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: bytes(payload))
    assert gamebuild.read_regulation_version(b"anything") == "11701000"


def test_undecryptable_regulation_raises(monkeypatch):
    def boom(blob):
        raise ValueError("bad padding")
    monkeypatch.setattr(gamebuild.regulation, "unpack", boom)
    with pytest.raises(GameBuildError, match="regulation"):
        gamebuild.read_regulation_version(b"anything")
