import subprocess

import pytest

from ermlib.formats import ooz


def test_library_builds_and_loads():
    """The .so is built from vendored sources on demand, with no system install."""
    path = ooz.library_path()
    assert path.exists()
    assert path.suffix == ".so"


def test_build_is_cached():
    """A second call must not rebuild — the sha256 of the sources is unchanged."""
    first = ooz.library_path()
    mtime = first.stat().st_mtime_ns
    second = ooz.library_path()
    assert second == first
    assert second.stat().st_mtime_ns == mtime


def test_decompress_rejects_a_short_result():
    """A truncated stream must raise, not return partial data. Silently short
    output would corrupt a merged archive in a way nothing downstream detects."""
    with pytest.raises(ooz.OozError):
        ooz.decompress(b"not a kraken stream", 4096)


def test_decompress_rejects_an_oversized_uncompressed_size():
    """DCX's uncompressed-size field is a bare u32 straight from the file --
    up to 4 GB, no validation against reality. Without a cap, a corrupted or
    hostile header sails through to ctypes.create_string_buffer and actually
    allocates that much. This must raise before any allocation happens, not
    partway through one."""
    with pytest.raises(ooz.OozError, match="refusing to allocate"):
        ooz.decompress(b"irrelevant", ooz.MAX_UNCOMPRESSED_SIZE + 1)


def test_library_path_builds_when_missing_and_caches_after(tmp_path, monkeypatch):
    """The compile branch (library_path's `if lib.exists() and stamp...`
    miss path) never runs in the rest of the suite, because tools/ooz/libooz.so
    is already built and cached there. Point BUILD_DIR at an empty temp dir so
    library_path() has nothing to find and must actually shell out to g++ --
    then prove the result is cached, and that invalidating the stamp forces a
    real second build rather than trusting a stale .so."""
    monkeypatch.setattr(ooz, "BUILD_DIR", tmp_path / "ooz-build")

    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ooz.subprocess, "run", spy)

    lib = ooz.library_path()
    assert lib.exists()
    assert len(calls) == 1  # missing .so and missing stamp forced a real compile

    stamp = ooz.BUILD_DIR / "build.sha256"
    assert stamp.read_text().strip() == ooz._source_digest()

    # Same digest, same .so already in place: must not rebuild.
    lib_again = ooz.library_path()
    assert lib_again == lib
    assert len(calls) == 1

    # A stamp that no longer matches the source digest (a stale build left
    # over from before a vendored-source edit) must trigger a real rebuild,
    # not get treated as still-valid because the .so happens to exist.
    stamp.write_text("stale-digest-not-matching-anything")
    lib_rebuilt = ooz.library_path()
    assert lib_rebuilt == lib
    assert len(calls) == 2
    assert stamp.read_text().strip() == ooz._source_digest()
