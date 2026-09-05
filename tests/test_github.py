import hashlib
import io

import pytest
from ermlib.github import pick_asset, download_verified, sha256_file, release_by_tag
from ermlib.errors import IntegrityError


class _RefusesWholeBodyRead:
    """A response that only answers sized reads.

    The pinned Nexus texture pack is 8 GB; `r.read()` with no size is what
    pulls all of it into one bytes object. A fake that refuses the unbounded
    call is the only way to prove the downloader streams without allocating
    gigabytes in a test.
    """

    def __init__(self, payload):
        self._buf = io.BytesIO(payload)
        self.biggest_read = 0

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("read the whole body into memory")
        self.biggest_read = max(self.biggest_read, size)
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _streaming(payload, seen=None):
    def _open(url):
        resp = _RefusesWholeBodyRead(payload)
        if seen is not None:
            seen.append(resp)
        return resp
    return _open


def test_pick_asset_selects_zip():
    rel = {"tag": "v1.9.8", "assets": [
        {"name": "notes.txt", "url": "u1", "digest": None},
        {"name": "Seamless.zip", "url": "u2", "digest": "sha256:abc"},
    ]}
    a = pick_asset(rel, suffix=".zip")
    assert a["name"] == "Seamless.zip"


def test_pick_asset_uses_name_hint():
    # me3's release ships two .zip assets; "first .zip" grabs the debug build.
    rel = {"tag": "v0.8.0", "assets": [
        {"name": "me3-debug-info.zip", "url": "u1", "digest": None},
        {"name": "me3-windows-amd64.zip", "url": "u2", "digest": None},
    ]}
    a = pick_asset(rel, name_hint="me3-windows-amd64")
    assert a["name"] == "me3-windows-amd64.zip"
    # No hint -> unchanged behavior, first .zip wins (backward compat).
    a_no_hint = pick_asset(rel)
    assert a_no_hint["name"] == "me3-debug-info.zip"


def test_pick_asset_hint_missing_falls_back():
    rel = {"tag": "v1.9.8", "assets": [
        {"name": "Seamless.zip", "url": "u2", "digest": "sha256:abc"},
    ]}
    a = pick_asset(rel, name_hint="no-such-asset")
    assert a["name"] == "Seamless.zip"


def test_download_verified_fails_closed(tmp_path, monkeypatch):
    import ermlib.github as gh
    monkeypatch.setattr(gh, "_urlopen", _streaming(b"payload"))
    dest = tmp_path / "mod.zip"
    with pytest.raises(IntegrityError):
        download_verified("http://x/mod.zip", dest, sha256="deadbeef")
    assert not dest.exists()          # partial file removed


def test_download_verified_ok(tmp_path, monkeypatch):
    import ermlib.github as gh
    payload = b"payload"
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(gh, "_urlopen", _streaming(payload))
    dest = tmp_path / "mod.zip"
    download_verified("http://x/mod.zip", dest, sha256=good)
    assert dest.read_bytes() == payload
    assert sha256_file(dest) == good


def test_release_by_tag_hits_the_tags_endpoint(monkeypatch):
    import ermlib.github as gh
    captured = {}

    def fake_fetch_json(url):
        captured["url"] = url
        return {
            "tag_name": "v1.9.8",
            "assets": [{"name": "Seamless.zip", "browser_download_url": "u2",
                        "digest": "sha256:abc"}],
        }

    monkeypatch.setattr(gh, "_fetch_json", fake_fetch_json)
    rel = release_by_tag(497113840, "v1.9.8")
    assert captured["url"] == \
        "https://api.github.com/repositories/497113840/releases/tags/v1.9.8"
    assert rel == {"tag": "v1.9.8",
                    "assets": [{"name": "Seamless.zip", "url": "u2", "digest": "sha256:abc"}]}


def test_both_http_clients_report_the_real_version():
    # One version literal, in __init__. A second hand-written one drifts the
    # moment __version__ is bumped, and nothing on the wire would say so.
    import ermlib
    from ermlib import nexus
    import ermlib.github as gh
    assert ermlib.__version__ in gh._UA["User-Agent"]
    assert ermlib.__version__ in nexus._headers("KEY")["User-Agent"]
    assert gh._UA["User-Agent"] == nexus._headers("KEY")["User-Agent"]


def test_download_streams_instead_of_buffering_the_whole_archive(tmp_path, monkeypatch):
    import ermlib.github as gh
    payload = b"a" * (3 << 20)
    seen = []
    monkeypatch.setattr(gh, "_urlopen", _streaming(payload, seen))
    dest = tmp_path / "mod.zip"

    digest = gh.download_stream("http://x/mod.zip", dest)

    assert dest.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    # Resident bytes stay bounded by one chunk however big the archive is.
    assert seen[0].biggest_read <= 1 << 20


def test_download_verified_also_streams(tmp_path, monkeypatch):
    import ermlib.github as gh
    payload = b"payload"
    monkeypatch.setattr(gh, "_urlopen", _streaming(payload))
    dest = tmp_path / "mod.zip"

    download_verified("http://x/mod.zip", dest, sha256=hashlib.sha256(payload).hexdigest())

    assert dest.read_bytes() == payload


def test_a_failed_download_leaves_no_partial_file(tmp_path, monkeypatch):
    import ermlib.github as gh
    monkeypatch.setattr(gh, "_urlopen", _streaming(b"payload"))
    dest = tmp_path / "mod.tar.gz"

    with pytest.raises(IntegrityError):
        download_verified("http://x/mod.tar.gz", dest, sha256="deadbeef")

    # Nothing half-written anywhere: not at dest, and not under whatever
    # temporary name the streaming write used.
    assert list(tmp_path.iterdir()) == []


def test_the_temp_name_keeps_the_real_extension(tmp_path, monkeypatch):
    # with_suffix() would turn me3-host-v0.13.0.tar.gz into ...tar.part, so a
    # download would land on a sibling archive that differs only past the
    # first dot. Watch what's on disk while the body is still being written.
    import ermlib.github as gh
    in_flight = []

    class _Watching(_RefusesWholeBodyRead):
        def read(self, size=-1):
            in_flight.extend(p.name for p in tmp_path.iterdir())
            return super().read(size)

    monkeypatch.setattr(gh, "_urlopen", lambda url: _Watching(b"payload"))
    dest = tmp_path / "me3-host-v0.13.0.tar.gz"
    gh.download_stream("http://x/a.tar.gz", dest)

    assert [p.name for p in tmp_path.iterdir()] == ["me3-host-v0.13.0.tar.gz"]
    assert in_flight and all(n.startswith("me3-host-v0.13.0.tar.gz")
                              for n in in_flight)
