import hashlib
import urllib.error
from pathlib import Path

import pytest

from ermlib import cli, manifest, github, nexus
from ermlib.errors import ErmError, IntegrityError


def _write_github_profile(base_dir, name="gh-only", mod_id="seamless-coop", repo_id=497113840):
    # A profile fixture with a single github-sourced mod, independent of the
    # real profiles/ directory — the production profiles now source
    # seamless-coop from Nexus, so github-path tests need their own mod
    # declaration to keep exercising github.py's fetch/pin/fail-closed logic.
    profiles_dir = Path(base_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test fixture: single github mod"\n'
        '\n'
        '[[mods]]\n'
        f'id = "{mod_id}"\n'
        'source = "github"\n'
        f'repo_id = {repo_id}\n'
        'kind = "coop-framework"\n'
    )
    return profiles_dir


def _write_mixed_profile(base_dir, name="mixed"):
    profiles_dir = Path(base_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test fixture: github + nexus mods"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
        '\n'
        '[[mods]]\n'
        'id = "elden-mod-loader"\n'
        'source = "nexus"\n'
        'nexus_id = 117\n'
        'kind = "loader"\n'
    )
    return profiles_dir


def test_fetch_downloads_github_and_prints_nexus(tmp_path, monkeypatch, capsys):
    # fake a GitHub release for ERSC
    monkeypatch.setattr(github, "latest_release", lambda rid: {
        "tag": "v1.9.8",
        "assets": [{"name": "Seamless.zip", "url": "http://x/Seamless.zip",
                    "digest": "sha256:" + "a" * 64}],
    })
    payload = b"zip-bytes"
    monkeypatch.setattr(github, "_fetch_bytes", lambda url: payload)
    monkeypatch.setattr(github, "download_verified",
                        lambda url, dest, sha256: Path(dest).write_bytes(payload))
    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    profiles_dir = _write_mixed_profile(tmp_path / "profiles")
    # nexus_api_key="" pinned explicitly: this test is about the no-key manual
    # path for nexus mods, regardless of whatever key this machine's real
    # secrets.env happens to have.
    updated = cli.fetch_profile("mixed", vendor, lock,
                                profiles_base=profiles_dir, nexus_api_key="")
    assert "seamless-coop" in updated                 # github mod locked
    out = capsys.readouterr().out
    assert "nexus" in out.lower()                      # manual step printed for elden-mod-loader
    assert manifest.load_lock(lock)["seamless-coop"]["version"] == "v1.9.8"
    assert "elden-mod-loader" not in manifest.load_lock(lock)  # manual mod never locked


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
    profiles_dir = _write_github_profile(tmp_path / "profiles")
    with pytest.raises(ErmError):
        cli.fetch_profile("gh-only", vendor, lock,
                           profiles_base=profiles_dir)


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
    profiles_dir = _write_github_profile(tmp_path / "profiles")
    updated = cli.fetch_profile("gh-only", vendor, lock_path,
                                profiles_base=profiles_dir, update=False)

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
    profiles_dir = _write_github_profile(tmp_path / "profiles")
    updated = cli.fetch_profile("gh-only", vendor, lock_path,
                                profiles_base=profiles_dir, update=True)

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
    profiles_dir = _write_github_profile(tmp_path / "profiles")
    with pytest.raises(IntegrityError):
        cli.fetch_profile("gh-only", vendor, lock_path,
                          profiles_base=profiles_dir, update=False)

    # Fail-closed: nothing landed on disk.
    assert list(vendor.iterdir()) == []


def _nexus_files_fixture():
    return [{"file_id": 45761, "version": "1.9.9", "category_name": "MAIN",
             "is_primary": False, "name": "main",
             "file_name": "Seamless Co-op v1.9.9-510-1-9-9-1776812412.zip",
             "uploaded_timestamp": 3000}]


def test_fetch_profile_nexus_with_key_first_fetch_pins_sha256(tmp_path, monkeypatch, capsys):
    # No prior lock entry: trust-on-first-use. The main file gets downloaded
    # and the sha256 we compute from what actually landed on disk is what
    # gets pinned — there's no upstream hash from Nexus to verify against yet.
    monkeypatch.setattr(nexus, "list_files", lambda mod_id, key: _nexus_files_fixture())
    monkeypatch.setattr(nexus, "download_url",
                        lambda mod_id, file_id, key: "https://cdn/x/Seamless%20Co-op.zip")

    def must_not_verify(url, dest, sha256):
        raise AssertionError(
            "download_verified must not run on a first fetch — there's no locked "
            "hash yet to verify against")
    monkeypatch.setattr(github, "download_verified", must_not_verify)

    payload = b"nexus-zip-bytes"
    monkeypatch.setattr(github, "_fetch_bytes", lambda url: payload)

    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    updated = cli.fetch_profile("seamless-only", vendor, lock,
                                profiles_base=Path("profiles"), nexus_api_key="TESTKEY")

    entry = updated["seamless-coop"]
    assert entry["version"] == "1.9.9"
    assert entry["source"] == "nexus"
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["asset"] == "Seamless Co-op v1.9.9-510-1-9-9-1776812412.zip"
    assert (vendor / entry["asset"]).read_bytes() == payload
    out = capsys.readouterr().out
    assert "fetched" in out.lower()


def test_fetch_profile_nexus_no_key_prints_manual_and_skips_api(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("nexus API must not be called when no key is present")
    monkeypatch.setattr(nexus, "list_files", boom)
    monkeypatch.setattr(nexus, "download_url", boom)

    vendor = tmp_path / "vendor"; vendor.mkdir()
    lock = tmp_path / "mods.lock.toml"
    updated = cli.fetch_profile("seamless-only", vendor, lock,
                                profiles_base=Path("profiles"), nexus_api_key="")

    assert updated == {}                       # nothing locked
    assert list(vendor.iterdir()) == []         # nothing downloaded
    out = capsys.readouterr().out
    assert "nexus" in out.lower()
    assert "manual" in out.lower()


def test_pinned_nexus_fetch_fails_closed_on_hash_mismatch(tmp_path, monkeypatch):
    # Mirrors test_pinned_fetch_fails_closed_on_hash_mismatch for the github
    # path: a pinned nexus fetch must verify against the LOCKED sha256, fail
    # closed, and leave nothing on disk if upstream doesn't match.
    lock_path = tmp_path / "mods.lock.toml"
    lock_path.write_text(
        '[seamless-coop]\n'
        'version = "1.9.9"\n'
        'asset = "Seamless Co-op v1.9.9-510-1-9-9-1776812412.zip"\n'
        f'sha256 = "{"a" * 64}"\n'
        'source = "nexus"\n'
    )

    monkeypatch.setattr(nexus, "list_files", lambda mod_id, key: _nexus_files_fixture())
    monkeypatch.setattr(nexus, "download_url",
                        lambda mod_id, file_id, key: "https://cdn/x/Seamless%20Co-op.zip")

    payload = b"a-mutated-upstream-payload"     # real sha will NOT equal locked "a"*64
    monkeypatch.setattr(github, "_fetch_bytes", lambda url: payload)  # real download_verified runs

    vendor = tmp_path / "vendor"; vendor.mkdir()
    with pytest.raises(IntegrityError):
        cli.fetch_profile("seamless-only", vendor, lock_path,
                          profiles_base=Path("profiles"), nexus_api_key="TESTKEY")

    assert list(vendor.iterdir()) == []
