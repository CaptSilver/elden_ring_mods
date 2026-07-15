import hashlib
import pytest
from ermlib.github import pick_asset, download_verified, sha256_file, release_by_tag
from ermlib.errors import IntegrityError


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
