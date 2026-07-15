import urllib.error
from pathlib import Path

import pytest

from ermlib import cli, manifest, github
from ermlib.errors import ErmError, IntegrityError


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


def _seed_lock(lock_path, sha="a" * 64):
    lock_path.write_text(
        '[seamless-coop]\n'
        'version = "v1.9.8"\n'
        'asset = "seamless-coop-v1.9.8.zip"\n'
        f'sha256 = "{sha}"\n'
        'source = "github"\n'
    )


def test_fetch_profile_honors_locked_version_by_default(tmp_path, monkeypatch):
    # A friend who clones the repo and runs `erm fetch` must get the PINNED
    # version, not whatever's newest — that's the whole point of the lockfile.
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path)
    calls = {"release_by_tag": [], "latest_release": []}

    def fake_release_by_tag(repo_id, tag):
        calls["release_by_tag"].append((repo_id, tag))
        return {"tag": tag, "assets": [{"name": "Seamless.zip",
                                         "url": "http://x/Seamless.zip", "digest": None}]}

    def fake_latest_release(repo_id):
        calls["latest_release"].append(repo_id)
        raise AssertionError("latest_release must not run when a pin exists and update=False")

    def fake_download_verified(url, dest, sha256):
        # The digest handed to download_verified on the pinned path must be the
        # LOCKED sha256 (the release's own digest was None here) — otherwise the
        # pin verifies nothing.
        assert sha256 == "a" * 64
        Path(dest).write_bytes(b"zip-bytes")

    monkeypatch.setattr(github, "release_by_tag", fake_release_by_tag)
    monkeypatch.setattr(github, "latest_release", fake_latest_release)
    monkeypatch.setattr(github, "download_verified", fake_download_verified)

    vendor = tmp_path / "vendor"; vendor.mkdir()
    updated = cli.fetch_profile("seamless-only", vendor, lock_path,
                                profiles_base=Path("profiles"), update=False)

    assert calls["release_by_tag"] == [(497113840, "v1.9.8")]
    assert calls["latest_release"] == []
    assert updated["seamless-coop"]["version"] == "v1.9.8"


def test_fetch_profile_update_true_repins_to_latest(tmp_path, monkeypatch):
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path)
    calls = {"release_by_tag": [], "latest_release": []}

    def fake_latest_release(repo_id):
        calls["latest_release"].append(repo_id)
        return {"tag": "v2.0.0", "assets": [{"name": "Seamless.zip",
                                              "url": "http://x/Seamless.zip",
                                              "digest": "sha256:" + "b" * 64}]}

    def fake_release_by_tag(repo_id, tag):
        calls["release_by_tag"].append((repo_id, tag))
        raise AssertionError("release_by_tag must not run when update=True")

    monkeypatch.setattr(github, "latest_release", fake_latest_release)
    monkeypatch.setattr(github, "release_by_tag", fake_release_by_tag)
    monkeypatch.setattr(github, "download_verified",
                        lambda url, dest, sha256: Path(dest).write_bytes(b"zip-bytes"))

    vendor = tmp_path / "vendor"; vendor.mkdir()
    updated = cli.fetch_profile("seamless-only", vendor, lock_path,
                                profiles_base=Path("profiles"), update=True)

    assert calls["latest_release"] == [497113840]
    assert calls["release_by_tag"] == []
    assert updated["seamless-coop"]["version"] == "v2.0.0"


def test_fetch_subparser_has_update_flag():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli.register(sub)
    args = parser.parse_args(["fetch", "seamless-only", "--update"])
    assert args.update is True
    args_default = parser.parse_args(["fetch"])
    assert args_default.update is False


def test_pinned_fetch_fails_closed_on_hash_mismatch(tmp_path, monkeypatch):
    # The security property behind the pin: on the pinned path the download is
    # verified against the LOCKED sha256, NOT the release's own digest. The
    # release digest here deliberately MATCHES the payload while the locked hash
    # does not — so if the code (wrongly) trusted the fresh digest the fetch
    # would succeed, and this test would go green. It stays red only because the
    # locked hash is what's enforced. Real download_verified runs (not mocked).
    import hashlib

    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, sha="a" * 64)  # will not match the payload

    payload = b"whatever"
    payload_hash = hashlib.sha256(payload).hexdigest()

    def fake_release_by_tag(repo_id, tag):
        # Release's own digest MATCHES the payload — the trap for a regression
        # that reverts to trusting asset.get("digest").
        return {"tag": tag, "assets": [{"name": "Seamless.zip",
                                         "url": "http://x/Seamless.zip",
                                         "digest": "sha256:" + payload_hash}]}

    monkeypatch.setattr(github, "release_by_tag", fake_release_by_tag)
    monkeypatch.setattr(github, "_fetch_bytes", lambda url: payload)

    vendor = tmp_path / "vendor"; vendor.mkdir()
    with pytest.raises(IntegrityError):
        cli.fetch_profile("seamless-only", vendor, lock_path,
                          profiles_base=Path("profiles"), update=False)

    # Fail-closed: nothing landed on disk.
    assert list(vendor.iterdir()) == []
