import zipfile
from pathlib import Path

from ermlib import cli, manifest, github, paths


def _seed_lock(lock_path, version="v1.9.8", sha="a" * 64):
    lock_path.write_text(
        '[seamless-coop]\n'
        f'version = "{version}"\n'
        f'asset = "seamless-coop-{version}.zip"\n'
        f'sha256 = "{sha}"\n'
        'source = "github"\n'
    )


def _make_ersc_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def _args(profile="seamless-only", json=False):
    return type("A", (), {"profile": profile, "json": json})()


def _seed_profile(tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "seamless-only.toml").write_text(
        'name = "seamless-only"\n'
        'description = "test profile"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
    )


def test_update_repins_and_installs_when_newer(tmp_path, monkeypatch, capsys):
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, version="v1.9.8", sha="a" * 64)

    game_dir = tmp_path / "Game"
    game_dir.mkdir()

    def fake_latest_release(repo_id):
        return {"tag": "v2.0.0", "assets": [{"name": "Seamless.zip",
                                              "url": "http://x/Seamless.zip",
                                              "digest": "sha256:" + "b" * 64}]}

    def fake_download_verified(url, dest, sha256):
        _make_ersc_zip(Path(dest))

    monkeypatch.setattr(github, "latest_release", fake_latest_release)
    monkeypatch.setattr(github, "download_verified", fake_download_verified)
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    _seed_profile(tmp_path)

    rc = cli.cmd_update(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert manifest.load_lock(lock_path)["seamless-coop"]["version"] == "v2.0.0"
    assert (game_dir / "ersc_launcher.exe").exists()
    assert (game_dir / "SeamlessCoop").is_dir()
    assert "LOCKSTEP" in out
    assert "v1.9.8 -> v2.0.0" in out


def test_update_noop_when_already_latest(tmp_path, monkeypatch, capsys):
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, version="v1.9.8", sha="a" * 64)

    def fake_latest_release(repo_id):
        return {"tag": "v1.9.8", "assets": [{"name": "Seamless.zip",
                                              "url": "http://x/Seamless.zip",
                                              "digest": "sha256:" + "a" * 64}]}

    def boom_find_steam_root():
        raise AssertionError("find_steam_root must not run when nothing changed")

    def fake_download_verified(url, dest, sha256):
        Path(dest).write_bytes(b"zip-bytes")

    monkeypatch.setattr(github, "latest_release", fake_latest_release)
    monkeypatch.setattr(github, "download_verified", fake_download_verified)
    monkeypatch.setattr(paths, "find_steam_root", boom_find_steam_root)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    _seed_profile(tmp_path)

    rc = cli.cmd_update(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "already latest" in out.lower() or "already up to date" in out.lower()
    assert not (tmp_path / "Game").exists()
