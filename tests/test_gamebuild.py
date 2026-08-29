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


def _payload(version=b"11701000"):
    payload = bytearray(b"BND4" + b"\x00" * 0x40)
    payload[0x18:0x20] = version
    return bytes(payload)


def test_a_repeat_read_of_the_same_regulation_does_not_decrypt_again(tmp_path, monkeypatch):
    # The whole point: decrypting the real 1.9 MB file costs seconds, and
    # `erm status` and `erm doctor` both want the answer on every run.
    calls = []
    monkeypatch.setattr(gamebuild.regulation, "unpack",
                        lambda blob: calls.append(blob) or _payload())
    cache = tmp_path / "stamps.json"
    assert gamebuild.cached_regulation_version(b"encrypted", cache) == "11701000"
    assert gamebuild.cached_regulation_version(b"encrypted", cache) == "11701000"
    assert len(calls) == 1


def test_a_changed_regulation_misses_the_cache(tmp_path, monkeypatch):
    cache = tmp_path / "stamps.json"
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: _payload(b"11601000"))
    assert gamebuild.cached_regulation_version(b"old bytes", cache) == "11601000"
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: _payload(b"11701000"))
    assert gamebuild.cached_regulation_version(b"new bytes", cache) == "11701000"


def test_a_corrupt_cache_is_rebuilt_rather_than_fatal(tmp_path, monkeypatch):
    cache = tmp_path / "stamps.json"
    cache.write_text("{not json")
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: _payload())
    assert gamebuild.cached_regulation_version(b"encrypted", cache) == "11701000"
    assert gamebuild.cached_regulation_version(b"encrypted", cache) == "11701000"


def test_a_cache_that_cannot_be_written_still_returns_the_right_answer(tmp_path, monkeypatch):
    blocked = tmp_path / "nowhere"
    blocked.write_text("i am a file, not a directory")
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: _payload())
    assert gamebuild.cached_regulation_version(
        b"encrypted", blocked / "stamps.json") == "11701000"


def test_identify_reads_the_stamp_through_the_cache(tmp_path, monkeypatch):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (game / "regulation.bin").write_bytes(b"encrypted")
    calls = []
    monkeypatch.setattr(gamebuild.regulation, "unpack",
                        lambda blob: calls.append(blob) or _payload())
    monkeypatch.setattr(gamebuild.steam, "read_appmanifest",
                        lambda root: {"buildid": "23850278"})
    monkeypatch.setattr(gamebuild, "CACHE_PATH", tmp_path / "stamps.json")
    assert gamebuild.identify(game, tmp_path).regulation == "11701000"
    assert gamebuild.identify(game, tmp_path).regulation == "11701000"
    assert len(calls) == 1


from ermlib.gamebuild import BuildId


def _build(**over):
    base = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
                steam_buildid="23850278", regulation_sha="a" * 64)
    base.update(over)
    return BuildId(**base)


def test_identify_assembles_from_all_three_sources(tmp_path, monkeypatch):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (game / "regulation.bin").write_bytes(b"encrypted")
    payload = bytearray(b"BND4" + b"\x00" * 0x40)
    payload[0x18:0x20] = b"11701000"
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: bytes(payload))
    monkeypatch.setattr(gamebuild.steam, "read_appmanifest",
                        lambda root: {"buildid": "23850278"})
    got = gamebuild.identify(game, tmp_path)
    assert got.exe == "2.7.0.0"
    assert got.app == "1.17.0"
    assert got.regulation == "11701000"
    assert got.steam_buildid == "23850278"
    assert len(got.regulation_sha) == 64


def test_identify_refuses_when_steam_has_no_buildid(tmp_path, monkeypatch):
    # An empty buildid would compare as a changed value and could flip a
    # tampered install into looking like a patched one.
    game = tmp_path / "Game"
    game.mkdir()
    (game / "eldenring.exe").write_bytes(_fake_exe(2, 7, 0, 0))
    (game / "regulation.bin").write_bytes(b"encrypted")
    payload = bytearray(b"BND4" + b"\x00" * 0x40)
    payload[0x18:0x20] = b"11701000"
    monkeypatch.setattr(gamebuild.regulation, "unpack", lambda blob: bytes(payload))
    monkeypatch.setattr(gamebuild.steam, "read_appmanifest", lambda root: {})
    with pytest.raises(gamebuild.GameBuildError, match="build id"):
        gamebuild.identify(game, tmp_path)


def test_drift_is_empty_when_nothing_moved():
    assert gamebuild.drift(_build(), _build()) == ()


def test_drift_names_every_field_that_moved():
    stamped = _build(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                     steam_buildid="1", regulation_sha="b" * 64)
    moved = {c.field for c in gamebuild.drift(stamped, _build())}
    assert moved == {"exe", "app", "regulation", "steam_buildid", "regulation_sha"}


def test_drift_against_an_unstamped_stack_is_empty():
    # A stack that was never stamped isn't "drifted" -- it's unknown. Callers
    # decide what to do; drift must not invent five changes out of nothing.
    assert gamebuild.drift(None, _build()) == ()


def test_classify_unchanged():
    assert gamebuild.classify(_build(), _build()) == gamebuild.UNCHANGED


def test_classify_patched_when_exe_and_regulation_move_together():
    stamped = _build(exe="2.6.2.0", regulation="11601000",
                     steam_buildid="1", regulation_sha="b" * 64)
    assert gamebuild.classify(stamped, _build()) == gamebuild.PATCHED


def test_classify_repackaged_when_only_the_exe_moves():
    # An EAC/launcher-only depot update. Nothing to rebuild -- but the stamp
    # must still be refreshed or every later run re-reports the same drift.
    stamped = _build(exe="2.6.2.0", steam_buildid="1")
    assert gamebuild.classify(stamped, _build()) == gamebuild.REPACKAGED


def test_classify_tampered_when_the_regulation_moves_alone():
    # exe and buildid identical, regulation content different: nobody patched
    # the game, something overwrote its regulation.bin.
    stamped = _build(regulation_sha="b" * 64)
    assert gamebuild.classify(stamped, _build()) == gamebuild.TAMPERED


def test_classify_tampered_when_the_regulation_version_moves_alone():
    stamped = _build(regulation="11601000", app="1.16.0", regulation_sha="b" * 64)
    assert gamebuild.classify(stamped, _build()) == gamebuild.TAMPERED


def test_classify_without_a_stamp_is_unchanged():
    assert gamebuild.classify(None, _build()) == gamebuild.UNCHANGED
