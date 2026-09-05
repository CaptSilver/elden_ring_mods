import pytest

from ermlib import manifest
from ermlib.errors import ErmError


def test_a_malformed_profile_raises_our_own_error(tmp_path):
    # A hand-edited profile with a syntax slip: tomllib's TOMLDecodeError is a
    # ValueError, so nothing between here and main()'s ErmError handler would
    # catch it and the user would get a bare traceback.
    (tmp_path / "broken.toml").write_text('[[mods]]\nid = "x"\nversion\n')
    with pytest.raises(ErmError) as exc:
        manifest.load_profile("broken", base=tmp_path)
    assert "broken.toml" in str(exc.value)


def test_a_malformed_lockfile_raises_our_own_error(tmp_path):
    # Merge conflict markers in the git-tracked lockfile look like this.
    lock = tmp_path / "mods.lock.toml"
    lock.write_text('<<<<<<< HEAD\n[mod]\nversion = "1"\n')
    with pytest.raises(ErmError) as exc:
        manifest.load_lock(lock)
    assert "mods.lock.toml" in str(exc.value)


def test_a_profile_that_is_not_utf8_raises_our_own_error(tmp_path):
    # read_text() raises UnicodeDecodeError, also a ValueError, and escapes
    # every handler the same way a decode error does.
    (tmp_path / "binary.toml").write_bytes(b'id = "\xff\xfe\x00"\n')
    with pytest.raises(ErmError):
        manifest.load_profile("binary", base=tmp_path)


def test_a_missing_profile_still_raises_oserror(tmp_path):
    # The CLI turns FileNotFoundError into "unknown profile '<name>'", so the
    # TOML guard must not swallow it.
    with pytest.raises(FileNotFoundError):
        manifest.load_profile("nope", base=tmp_path)


def test_a_missing_lockfile_is_still_an_empty_dict(tmp_path):
    assert manifest.load_lock(tmp_path / "absent.toml") == {}
