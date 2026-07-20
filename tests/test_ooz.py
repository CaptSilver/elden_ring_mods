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
