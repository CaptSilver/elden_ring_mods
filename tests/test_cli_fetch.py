from pathlib import Path
from ermlib import cli, manifest, github


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
