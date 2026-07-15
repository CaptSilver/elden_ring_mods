import hashlib
import pytest
from ermlib.github import pick_asset, download_verified, sha256_file
from ermlib.errors import IntegrityError


def test_pick_asset_selects_zip():
    rel = {"tag": "v1.9.8", "assets": [
        {"name": "notes.txt", "url": "u1", "digest": None},
        {"name": "Seamless.zip", "url": "u2", "digest": "sha256:abc"},
    ]}
    a = pick_asset(rel, suffix=".zip")
    assert a["name"] == "Seamless.zip"


def test_download_verified_fails_closed(tmp_path, monkeypatch):
    import ermlib.github as gh
    monkeypatch.setattr(gh, "_fetch_bytes", lambda url: b"payload")
    dest = tmp_path / "mod.zip"
    with pytest.raises(IntegrityError):
        download_verified("http://x/mod.zip", dest, sha256="deadbeef")
    assert not dest.exists()          # partial file removed


def test_download_verified_ok(tmp_path, monkeypatch):
    import ermlib.github as gh
    payload = b"payload"
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(gh, "_fetch_bytes", lambda url: payload)
    dest = tmp_path / "mod.zip"
    download_verified("http://x/mod.zip", dest, sha256=good)
    assert dest.read_bytes() == payload
    assert sha256_file(dest) == good
