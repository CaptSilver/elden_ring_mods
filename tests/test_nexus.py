import urllib.error

import pytest

from ermlib import nexus
from ermlib.errors import ErmError


def _files_fixture():
    # Mirrors the real shape Nexus returns for Seamless Co-op #510: a current
    # MAIN file plus older MAIN releases relegated to OLD_VERSION, and an
    # unrelated ARCHIVED file. is_primary is False on the real MAIN file too
    # (must not be relied on).
    return [
        {"file_id": 40000, "version": "1.9.7", "category_name": "OLD_VERSION",
         "is_primary": False, "name": "old", "file_name": "old.zip",
         "uploaded_timestamp": 1000},
        {"file_id": 41000, "version": "1.9.8", "category_name": "OLD_VERSION",
         "is_primary": False, "name": "old2", "file_name": "old2.zip",
         "uploaded_timestamp": 2000},
        {"file_id": 45761, "version": "1.9.9", "category_name": "MAIN",
         "is_primary": False, "name": "main",
         "file_name": "Seamless Co-op v1.9.9-510-1-9-9-1776812412.zip",
         "uploaded_timestamp": 3000},
        {"file_id": 39999, "version": "1.0.0", "category_name": "ARCHIVED",
         "is_primary": False, "name": "archived", "file_name": "archived.zip",
         "uploaded_timestamp": 500},
    ]


def test_pick_main_file_selects_main_excludes_old_and_archived_highest_version():
    f = nexus.pick_main_file(_files_fixture())
    assert f["file_id"] == 45761
    assert f["version"] == "1.9.9"


def test_pick_main_file_raises_when_no_main():
    files = [f for f in _files_fixture() if f["category_name"] != "MAIN"]
    with pytest.raises(ErmError):
        nexus.pick_main_file(files)


def test_find_file_by_version_returns_the_matching_main_file():
    f = nexus.find_file_by_version(_files_fixture(), "1.9.9")
    assert f["file_id"] == 45761


def test_find_file_by_version_raises_when_absent():
    with pytest.raises(ErmError, match="9.9.9"):
        nexus.find_file_by_version(_files_fixture(), "9.9.9")


def test_main_files_returns_only_main_category():
    mains = nexus.main_files(_files_fixture())
    assert [f["file_id"] for f in mains] == [45761]


def test_main_files_returns_all_when_several():
    # Minimal HUD #148-style case: several variant files all tagged MAIN.
    files = _files_fixture() + [
        {"file_id": 46000, "version": "1.9.9", "category_name": "MAIN",
         "is_primary": False, "name": "main-variant",
         "file_name": "Seamless Co-op variant.zip", "uploaded_timestamp": 4000},
    ]
    mains = nexus.main_files(files)
    assert {f["file_id"] for f in mains} == {45761, 46000}


def test_find_file_by_id_returns_the_matching_file():
    f = nexus.find_file_by_id(_files_fixture(), 45761)
    assert f["file_name"] == "Seamless Co-op v1.9.9-510-1-9-9-1776812412.zip"


def test_find_file_by_id_raises_when_absent():
    with pytest.raises(ErmError, match="99999"):
        nexus.find_file_by_id(_files_fixture(), 99999)


def test_download_url_percent_encodes_spaces_and_preserves_query(monkeypatch):
    mirrors = [{"short_name": "Nexus CDN",
                "URI": "https://cdn/4333/510/Seamless Co-op v1.9.9.zip"
                       "?expires=1&md5=x&user_id=2"}]
    monkeypatch.setattr(nexus, "_api_get", lambda path, api_key: mirrors)
    url = nexus.download_url(510, 45761, "TESTKEY")
    assert "%20" in url
    assert " " not in url
    assert url.endswith("?expires=1&md5=x&user_id=2")
    assert url.startswith("https://cdn/4333/510/Seamless%20Co-op%20v1.9.9.zip")


def test_download_url_prefers_nexus_cdn_mirror(monkeypatch):
    mirrors = [
        {"short_name": "Other Mirror", "URI": "https://other/x.zip"},
        {"short_name": "Nexus CDN", "URI": "https://cdn/x.zip"},
    ]
    monkeypatch.setattr(nexus, "_api_get", lambda path, api_key: mirrors)
    assert nexus.download_url(510, 45761, "TESTKEY") == "https://cdn/x.zip"


def test_download_url_falls_back_to_first_mirror_when_no_nexus_cdn(monkeypatch):
    mirrors = [{"short_name": "Some Mirror", "URI": "https://mirror1/x.zip"}]
    monkeypatch.setattr(nexus, "_api_get", lambda path, api_key: mirrors)
    assert nexus.download_url(510, 45761, "TESTKEY") == "https://mirror1/x.zip"


def _http_error(code):
    def boom(req):
        raise urllib.error.HTTPError(req.full_url, code, "error", {}, None)
    return boom


def test_api_get_401_raises_invalid_key_error(monkeypatch):
    monkeypatch.setattr(nexus, "_urlopen", _http_error(401))
    with pytest.raises(ErmError, match="invalid"):
        nexus._api_get("/games/eldenring/mods/510/files.json", "BADKEY")


def test_api_get_403_raises_premium_error(monkeypatch):
    monkeypatch.setattr(nexus, "_urlopen", _http_error(403))
    with pytest.raises(ErmError, match="[Pp]remium"):
        nexus._api_get("/games/eldenring/mods/510/files/45761/download_link.json", "FREEKEY")


def test_api_get_429_raises_rate_limit_error(monkeypatch):
    monkeypatch.setattr(nexus, "_urlopen", _http_error(429))
    with pytest.raises(ErmError, match="rate limit"):
        nexus._api_get("/games/eldenring/mods/510/files.json", "KEY")


def test_api_get_unreachable_raises_erm_error(monkeypatch):
    def boom(req):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(nexus, "_urlopen", boom)
    with pytest.raises(ErmError, match="unreachable"):
        nexus._api_get("/games/eldenring/mods/510/files.json", "KEY")


def test_headers_never_include_the_key_in_a_readable_log_field():
    # Not a security proof, just a guard against an obvious mistake: the key
    # must go in the apikey header and nowhere else (e.g. User-Agent).
    h = nexus._headers("SUPERSECRETKEY")
    assert h["apikey"] == "SUPERSECRETKEY"
    assert "SUPERSECRETKEY" not in h["User-Agent"]
    assert "SUPERSECRETKEY" not in h["Application-Name"]
    assert "SUPERSECRETKEY" not in h["Application-Version"]
