import zipfile
from pathlib import Path

from ermlib import cli, manifest, github, paths, me3profile
from ermlib import state as state_mod


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
    # `erm update` installs via the same path as `erm apply` — the doctor
    # safety check must run right after that install too, not just on apply.
    assert "doctor" in out.lower()
    assert "no proxy dll" in out.lower()


def test_update_reconciles_me3_profile_after_installing_seamless(tmp_path, monkeypatch, capsys):
    # Starting state: a me3-package mod is installed (cosmetic-extras-style
    # profile) but seamless-coop is NOT in state yet, so erm-coop.me3 has no
    # [[natives]] entry. `erm update` installs seamless-coop via
    # _install_ersc, which writes state directly — but cmd_update never
    # called me3profile.reconcile, so the profile file kept its stale
    # no-natives form and me3 would never chainload ersc.dll.
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

    # Seed installed.json + erm-coop.me3 as they'd look after applying a
    # me3-package-only profile: a package recorded, no seamless-coop.
    me3_dir = tmp_path / "tools" / "me3"
    pkg_dir = me3_dir / "mods" / "unit-mod"
    pkg_dir.mkdir(parents=True)
    state = {}
    state_mod.record_me3_package(state, "unit-mod", "1.0", "unit-mod.zip",
                                  str(Path("tools") / "me3" / "mods" / "unit-mod"))
    state_mod.write_state(tmp_path / "installed.json", state)
    me3profile.reconcile(state, me3_dir, game_dir)
    prof = me3_dir / "erm-coop.me3"
    assert "[[natives]]" not in prof.read_text()   # sanity: no chainload yet

    rc = cli.cmd_update(_args())
    capsys.readouterr()

    assert rc == 0
    assert "[[natives]]" in prof.read_text()   # reconcile ran with the post-install state


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


def _seed_profile_with_nexus(tmp_path):
    """seamless-only plus a Nexus mod. Without a NEXUS_API_KEY the Nexus mod
    takes the manual-download branch, so erm never contacts upstream for it."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / "with-nexus.toml").write_text(
        'name = "with-nexus"\n'
        'description = "test profile"\n'
        '\n[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop"\n'
        '\n[[mods]]\n'
        'id = "hand-download"\n'
        'source = "nexus"\n'
        'nexus_id = 42\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n')


def _stub_github_latest(monkeypatch, tag="v1.9.8"):
    monkeypatch.setattr(github, "latest_release", lambda repo_id: {
        "tag": tag, "assets": [{"name": "Seamless.zip", "url": "http://x/Seamless.zip",
                                "digest": "sha256:" + "a" * 64}]})
    monkeypatch.setattr(github, "download_verified",
                        lambda url, dest, sha256: Path(dest).write_bytes(b"zip-bytes"))


def test_update_does_not_report_lockfile_entries_the_profile_never_names(
        tmp_path, monkeypatch, capsys):
    """The lockfile is shared across every profile, so it always carries more
    entries than the one being updated. Reporting them meant asserting a
    version was current for mods erm never contacted upstream about."""
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, version="v1.9.8", sha="a" * 64)
    lock_path.write_text(lock_path.read_text() +
        '\n[other-profiles-mod]\nversion = "3.0"\nasset = "o.zip"\n'
        'sha256 = "b"\nsource = "nexus"\n')

    _stub_github_latest(monkeypatch)
    monkeypatch.setattr(paths, "find_steam_root", lambda: (_ for _ in ()).throw(
        AssertionError("find_steam_root must not run when nothing changed")))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    _seed_profile(tmp_path)

    assert cli.cmd_update(_args()) == 0
    out = capsys.readouterr().out

    assert "other-profiles-mod" not in out, (
        "reported a mod that isn't in the profile and was never contacted")


def test_update_does_not_claim_a_manual_download_is_already_latest(
        tmp_path, monkeypatch, capsys):
    """A Nexus mod with no API key is skipped with a manual-download notice --
    erm never asked upstream what the latest version is. Calling it 'already
    latest' in the same report contradicts the notice directly above it."""
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, version="v1.9.8", sha="a" * 64)
    lock_path.write_text(lock_path.read_text() +
        '\n[hand-download]\nversion = "3.0"\nasset = "h.zip"\n'
        'sha256 = "b"\nsource = "nexus"\n')

    _stub_github_latest(monkeypatch)
    monkeypatch.setattr(paths, "find_steam_root", lambda: (_ for _ in ()).throw(
        AssertionError("find_steam_root must not run when nothing changed")))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    _seed_profile_with_nexus(tmp_path)

    assert cli.cmd_update(_args(profile="with-nexus")) == 0
    out = capsys.readouterr().out

    assert "manual Nexus download" in out
    assert "hand-download already latest" not in out, (
        "claimed a version is current for a mod erm declined to check")
    assert "Already up to date — nothing to install." not in out, (
        "the closing summary contradicts the manual-download warning above it")


def test_update_json_is_one_document_carrying_the_lockstep_warning(
        tmp_path, monkeypatch, capsys):
    # update printed its report, then "Installed seamless-coop …", then the
    # LOCKSTEP line, then "Safety check (erm doctor):", then a second document.
    # The LOCKSTEP warning is the one thing a co-op partner must not miss, so
    # it has to be IN the document rather than prose wrapped around it.
    lock_path = tmp_path / "mods.lock.toml"
    _seed_lock(lock_path, version="v1.9.8", sha="a" * 64)
    game_dir = tmp_path / "Game"
    game_dir.mkdir()

    monkeypatch.setattr(github, "latest_release", lambda repo_id: {
        "tag": "v2.0.0", "assets": [{"name": "Seamless.zip",
                                     "url": "http://x/Seamless.zip",
                                     "digest": "sha256:" + "b" * 64}]})
    monkeypatch.setattr(github, "download_verified",
                        lambda url, dest, sha256: _make_ersc_zip(Path(dest)))
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    _seed_profile(tmp_path)

    import json as _json
    rc = cli.cmd_update(_args(json=True))
    data = _json.loads(capsys.readouterr().out)

    assert rc == 0
    assert any("LOCKSTEP" in i["message"] for i in data["items"])
    assert any("v1.9.8 -> v2.0.0" in i["message"] for i in data["items"])
    # The fake Game/ has no regulation.bin, so the nested doctor cannot identify
    # a build and warns -- which is the proof that it ran the build checks at all.
    assert data["doctor"]["worst"] == "warn"
    assert any("game build" in i["message"] for i in data["doctor"]["items"]), (
        "the nested doctor must run the same checks erm doctor runs")
