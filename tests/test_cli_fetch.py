import urllib.error
from pathlib import Path

import pytest

from ermlib import cli, manifest, github
from ermlib.errors import ErmError


def test_fetch_downloads_github_and_prints_nexus(tmp_path, monkeypatch, capsys):
    # fake a GitHub release for ERSC
    monkeypatch.setattr(github, "latest_release", lambda rid: {
        "tag": "v1.9.8",
        "assets": [{"name": "Seamless.zip", "url": "http://x/Seamless.zip",
                    "digest": "sha256:" + "a" * 64}],
    })
    import hashlib
    payload = b"zip-bytes"
    monkeypatch.setattr(github, "_fetch_bytes", lambda url: payload)
    monkeypatch.setattr(github, "download_verified",
                        lambda url, dest, sha256: Path(dest).write_bytes(payload))
    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    updated = cli.fetch_profile("seamless-extras", vendor, lock,
                                profiles_base=Path("profiles"))
    assert "seamless-coop" in updated                 # github mod locked
    out = capsys.readouterr().out
    assert "nexus" in out.lower()                      # manual step printed for #117 etc.
    assert manifest.load_lock(lock)["seamless-coop"]["version"] == "v1.9.8"


def test_fetch_profile_unknown_profile_raises_clean_error(tmp_path):
    # Typo'd profile name -> manifest.load_profile's read_text raises a raw
    # FileNotFoundError today. Must surface as a clean ErmError.
    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    with pytest.raises(ErmError):
        cli.fetch_profile("no-such-profile-xyz", vendor, lock,
                           profiles_base=Path("profiles"))


def test_fetch_profile_network_failure_raises_clean_error(tmp_path, monkeypatch):
    # A GitHub API/network failure must not leak urllib.error.URLError raw.
    def boom(repo_id):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(github, "latest_release", boom)
    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    with pytest.raises(ErmError):
        cli.fetch_profile("seamless-only", vendor, lock,
                           profiles_base=Path("profiles"))
