import json
import shutil
import zipfile
from pathlib import Path

import pytest

from ermlib import cli, harden, me3profile, paths
from ermlib.errors import ErmError, NetworkError, PathError
from tests.conftest import REPO


def _write_profile(profiles_dir, name, mods_toml, excludes=None):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    lines = [f'name = "{name}"', 'description = "test fixture profile"']
    if excludes:
        lines.append("excludes = [" + ", ".join(f'"{e}"' for e in excludes) + "]")
    (profiles_dir / f"{name}.toml").write_text("\n".join(lines) + "\n\n" + mods_toml)


def _seed_lock(lock_path, entries):
    # entries: {mod_id: (version, asset)}
    lines = []
    for mid, (version, asset) in entries.items():
        lines.append(f'[{mid}]')
        lines.append(f'version = "{version}"')
        lines.append(f'asset = "{asset}"')
        lines.append('sha256 = "a"')
        lines.append('source = "github"')
        lines.append('')
    lock_path.write_text("\n".join(lines))


def _zip_with(path, member, data=b"\x00"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, data)


def _apply_args(profile, json_out=False):
    return type("A", (), {"profile": profile, "json": json_out})()


def _uninstall_args(mod, json_out=False):
    return type("A", (), {"mod": mod, "json": json_out})()


_TWO_MOD_PLUS_MANUAL = (
    '[[mods]]\n'
    'id = "mod-a"\n'
    'source = "github"\n'
    'repo_id = 1\n'
    'kind = "test"\n'
    'install = "game"\n'
    '\n'
    '[[mods]]\n'
    'id = "mod-b"\n'
    'source = "github"\n'
    'repo_id = 2\n'
    'kind = "test"\n'
    'install = "mods"\n'
    '\n'
    '[[mods]]\n'
    'id = "me3"\n'
    'source = "github"\n'
    'repo_id = 3\n'
    'kind = "loader"\n'
    'install = "manual"\n'
)


def _seed_two_mod_profile(tmp_path):
    """mod-a ships its own mods/ folder inside the zip (install=game);
    mod-b is a bare DLL that erm has to place under mods/ itself
    (install=mods); me3 is manual and never fetched-locked here at all —
    manual mods must never even be consulted in the lockfile."""
    _write_profile(tmp_path / "profiles", "two-mod", _TWO_MOD_PLUS_MANUAL)
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "mod-b": ("1.0", "mod-b.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "mods/x.dll")
    _zip_with(vendor / "mod-b.zip", "y.dll")


def test_profile_needs_fetch_detects_missing_and_ignores_manual(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vendor").mkdir()
    profile = {"mods": [
        {"id": "have", "install": "game"},
        {"id": "manual-mod", "install": "manual"},
        {"id": "missing", "install": "mods"},
    ]}
    (tmp_path / "vendor" / "have.zip").write_bytes(b"x")
    lock = {"have": {"asset": "have.zip"}}
    assert cli._profile_needs_fetch(profile, lock) is True    # 'missing' has no lock entry
    lock["missing"] = {"asset": "missing.zip"}
    assert cli._profile_needs_fetch(profile, lock) is True    # 'missing' vendor file absent
    (tmp_path / "vendor" / "missing.zip").write_bytes(b"y")
    assert cli._profile_needs_fetch(profile, lock) is False   # all present; manual ignored


def test_apply_auto_fetches_missing_mod_then_installs(tmp_path, monkeypatch, capsys, tmp_game):
    # A mod that isn't fetched yet must be fetched automatically (only_missing=True),
    # then installed — no separate `erm fetch` step required.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path / "profiles", "needs-fetch",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "mods"\n'
    )
    (tmp_path / "vendor").mkdir()   # empty: mod-a not fetched yet

    calls = {}
    def fake_fetch(profile_name, vendor, lock_path, only_missing=False, **kw):
        calls["profile"] = profile_name
        calls["only_missing"] = only_missing
        _zip_with(Path(vendor) / "mod-a.zip", "a.dll")   # simulate the download
        return {"mod-a": {"version": "1.0", "asset": "mod-a.zip",
                          "sha256": "a", "source": "github"}}
    monkeypatch.setattr(cli, "fetch_profile", fake_fetch)

    rc = cli.cmd_apply(_apply_args("needs-fetch"))
    capsys.readouterr()

    assert rc == 0
    assert calls == {"profile": "needs-fetch", "only_missing": True}
    assert (game_dir / "mods" / "a.dll").exists()    # fetched, then installed
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" in state


def test_apply_skips_auto_fetch_when_all_present(tmp_path, monkeypatch, capsys, tmp_game):
    # Everything already on disk -> no auto-fetch (a fetched profile applies
    # offline). The spy raises if fetch_profile is called at all.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.setattr(harden, "set_immutable", lambda p, o: None)
    monkeypatch.chdir(tmp_path)
    _seed_two_mod_profile(tmp_path)   # mod-a, mod-b fetched (lock+vendor); me3 is manual

    def boom(*a, **k):
        raise AssertionError("auto-fetch must not run when nothing is missing")
    monkeypatch.setattr(cli, "fetch_profile", boom)

    rc = cli.cmd_apply(_apply_args("two-mod"))
    capsys.readouterr()

    assert rc == 0
    assert (game_dir / "mods" / "x.dll").exists()    # installed from the present vendor archive


def test_apply_auto_fetch_failure_warns_and_installs_present(tmp_path, monkeypatch, capsys, tmp_game):
    # Auto-fetch hitting a network error must NOT abort apply: warn, then install
    # whatever's already present. mod-a is fetched, mod-b is not.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path / "profiles", "partial",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "mods"\n'
        '\n'
        '[[mods]]\n'
        'id = "mod-b"\n'
        'source = "github"\n'
        'repo_id = 2\n'
        'kind = "test"\n'
        'install = "mods"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"mod-a": ("1.0", "mod-a.zip")})   # only mod-a locked
    vendor = tmp_path / "vendor"; vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")   # mod-a present, mod-b missing -> triggers fetch

    def fake_fetch(*a, **k):
        raise NetworkError("no route to host")
    monkeypatch.setattr(cli, "fetch_profile", fake_fetch)

    rc = cli.cmd_apply(_apply_args("partial"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "auto-fetch incomplete" in out.lower()
    assert (game_dir / "mods" / "a.dll").exists()     # present mod still installed
    assert "mod-b" in out                              # still-missing mod warned about
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" in state and "mod-b" not in state


def test_apply_two_mod_profile_installs_game_and_mods_targets_and_skips_manual(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    # the "two-mod" fixture's me3 entry is kind="loader" -> apply now auto-hardens;
    # mock set_immutable so this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)
    _seed_two_mod_profile(tmp_path)

    rc = cli.cmd_apply(_apply_args("two-mod"))
    out = capsys.readouterr().out

    assert rc == 0
    # mod-a's own archive already ships a mods/ folder -> lands under Game/mods/
    assert (game_dir / "mods" / "x.dll").exists()
    # mod-b is a bare DLL -> erm places it under Game/mods/ itself
    assert (game_dir / "mods" / "y.dll").exists()
    assert "me3" in out and "manual" in out.lower()

    state = json.loads((tmp_path / "installed.json").read_text())
    assert state["mod-a"]["files"] == ["mods/x.dll"]
    assert state["mod-b"]["files"] == ["mods/y.dll"]
    assert "me3" not in state       # manual mods are never recorded


def test_apply_seamless_only_backward_compat_uses_real_profile(
        tmp_path, monkeypatch, capsys, tmp_game):
    # Copy the REAL profiles/ dir (carrying the install= field this change
    # added) so this exercises the actual production seamless-only.toml, not
    # a hand-written stand-in — proves the file I edited actually works.
    shutil.copytree(REPO / "profiles", tmp_path / "profiles")
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _seed_lock(tmp_path / "mods.lock.toml",
               {"seamless-coop": ("v1.9.8", "seamless-coop-v1.9.8.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "seamless-coop-v1.9.8.zip", "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")

    rc = cli.cmd_apply(_apply_args("seamless-only"))   # bare `erm apply` default
    out = capsys.readouterr().out

    assert rc == 0
    assert (game_dir / "ersc_launcher.exe").exists()
    assert (game_dir / "SeamlessCoop" / "ersc_settings.ini").exists()
    assert "doctor" in out.lower()
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "seamless-coop" in state
    assert "ersc_launcher.exe" in state["seamless-coop"]["files"]


def test_uninstall_profile_removes_all_its_mods_prunes_dirs_and_spares_stock(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    # the "two-mod" fixture's me3 entry is kind="loader" -> apply now auto-hardens;
    # mock set_immutable so this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)
    _seed_two_mod_profile(tmp_path)

    cli.cmd_apply(_apply_args("two-mod"))
    capsys.readouterr()
    assert (game_dir / "mods" / "x.dll").exists()
    assert (game_dir / "mods" / "y.dll").exists()

    rc = cli.cmd_uninstall(_uninstall_args("two-mod"))
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "mods" / "x.dll").exists()
    assert not (game_dir / "mods" / "y.dll").exists()
    assert not (game_dir / "mods").exists()          # emptied dir pruned
    assert (game_dir / "eldenring.exe").exists()      # stock file survives
    assert (game_dir / "start_protected_game.exe").exists()
    assert "manual" in out.lower()                    # me3 noted, not force-uninstalled

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" not in state
    assert "mod-b" not in state


def test_switch_uninstalls_current_profile_and_applies_the_new_one(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "profile-a",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "game"\n'
    )
    _write_profile(tmp_path / "profiles", "profile-b",
        '[[mods]]\n'
        'id = "mod-c"\n'
        'source = "github"\n'
        'repo_id = 3\n'
        'kind = "test"\n'
        'install = "game"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "mod-c": ("1.0", "mod-c.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")
    _zip_with(vendor / "mod-c.zip", "c.dll")

    cli.cmd_apply(_apply_args("profile-a"))
    capsys.readouterr()
    assert (game_dir / "a.dll").exists()
    before = json.loads((tmp_path / "installed.json").read_text())
    assert list(before.keys()) == ["mod-a"]

    rc = cli.cmd_switch(type("A", (), {"profile": "profile-b", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "a.dll").exists()          # profile A's mod gone
    assert (game_dir / "c.dll").exists()               # profile B's mod installed
    assert (game_dir / "eldenring.exe").exists()        # stock file survives
    assert "switching to profile-b" in out.lower()

    after = json.loads((tmp_path / "installed.json").read_text())
    assert list(after.keys()) == ["mod-c"]


_TWO_MOD_ONE_CORRUPT = (
    '[[mods]]\n'
    'id = "mod-a"\n'
    'source = "github"\n'
    'repo_id = 1\n'
    'kind = "test"\n'
    'install = "mods"\n'
    '\n'
    '[[mods]]\n'
    'id = "mod-b"\n'
    'source = "github"\n'
    'repo_id = 2\n'
    'kind = "test"\n'
    'install = "mods"\n'
)


def test_apply_corrupt_archive_warns_and_earlier_mod_stays_recorded(
        tmp_path, monkeypatch, capsys, tmp_game):
    # mod-a is a valid zip, mod-b is garbage bytes. A BadZipFile on mod-b must
    # NOT abort the whole apply: mod-a (installed earlier in the same run) must
    # still land on disk AND be recorded in installed.json — so write_state has
    # to run even though a later mod blew up.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "corrupt", _TWO_MOD_ONE_CORRUPT)
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "mod-b": ("1.0", "mod-b.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")
    (vendor / "mod-b.zip").write_bytes(b"this is not a zip file")

    rc = cli.cmd_apply(_apply_args("corrupt"))
    out = capsys.readouterr().out

    assert rc == 0                                   # clean exit, no raw traceback
    assert "mod-b" in out and "failed" in out.lower()
    assert (game_dir / "mods" / "a.dll").exists()     # earlier mod really installed
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" in state                          # earlier mod stays recorded
    assert "mod-b" not in state                      # the one that failed is not


def _seed_exclude_pair(tmp_path, installed_state=None):
    """A gameplay-extras-like profile (one me3-package mod, clevers-moveset) and a
    randomizer-like profile that excludes it — mirrors the real
    gameplay-extras/randomizer pair without depending on the real profiles/ dir."""
    _write_profile(tmp_path / "profiles", "gameplay-extras-like",
        '[[mods]]\n'
        'id = "clevers-moveset"\n'
        'source = "nexus"\n'
        'nexus_id = 1928\n'
        'kind = "overhaul"\n'
        'install = "me3-package"\n',
        excludes=["randomizer-like"])
    _write_profile(tmp_path / "profiles", "randomizer-like",
        '[[mods]]\n'
        'id = "item-enemy-randomizer"\n'
        'source = "nexus"\n'
        'nexus_id = 428\n'
        'kind = "randomizer"\n'
        'install = "randomizer"\n',
        excludes=["gameplay-extras-like"])
    _seed_lock(tmp_path / "mods.lock.toml",
               {"item-enemy-randomizer": ("1.0", "randomizer.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "randomizer.zip", "randomizer/EldenRingRandomizer.exe")
    if installed_state is not None:
        (tmp_path / "installed.json").write_text(json.dumps(installed_state))


def test_apply_refuses_when_excluded_profile_mod_is_installed(
        tmp_path, monkeypatch, capsys, tmp_game):
    # clevers-moveset (gameplay-extras-like) is already installed. Applying
    # randomizer-like — which excludes gameplay-extras-like — must refuse
    # BEFORE touching disk: no tools/ extraction, no installed.json change.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_exclude_pair(tmp_path, installed_state={
        "clevers-moveset": {"version": "25.0", "archive": "clevers.zip",
                             "kind": "me3-package", "package": "tools/me3/mods/clevers-moveset"},
    })

    with pytest.raises(ErmError) as excinfo:
        cli.cmd_apply(_apply_args("randomizer-like"))
    capsys.readouterr()

    msg = str(excinfo.value)
    assert "randomizer-like" in msg
    assert "gameplay-extras-like" in msg
    assert "clevers-moveset" in msg

    assert not (tmp_path / "tools" / "item-enemy-randomizer").exists()  # nothing extracted
    state = json.loads((tmp_path / "installed.json").read_text())
    assert list(state.keys()) == ["clevers-moveset"]                    # unchanged


def test_apply_excludes_gate_passes_with_empty_state(
        tmp_path, monkeypatch, capsys, tmp_game):
    # With nothing installed, the excludes check must not trip even though the
    # profile declares excludes=["gameplay-extras-like"] — apply proceeds normally.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.setattr(paths, "find_proton", lambda: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "steamapps" / "compatdata" / paths.APPID).mkdir(parents=True)
    _seed_exclude_pair(tmp_path)   # no installed.json at all -> load_state() returns {}

    rc = cli.cmd_apply(_apply_args("randomizer-like"))   # must NOT raise (proves the
    capsys.readouterr()                                  # excludes gate alone passed)

    assert rc == 0
    state = json.loads((tmp_path / "installed.json").read_text())
    assert "clevers-moveset" not in state


def test_apply_refuses_when_recorded_randomizer_blocks_excluding_profile(
        tmp_path, monkeypatch, capsys, tmp_game):
    # The reverse direction: the randomizer generator is recorded in
    # installed.json (kind="randomizer"), so applying gameplay-extras-like —
    # which excludes randomizer-like — must refuse before touching disk. Proves
    # recording the randomizer makes the mutual exclusion work BOTH ways.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_exclude_pair(tmp_path, installed_state={
        "item-enemy-randomizer": {"version": "1.0", "archive": "randomizer.zip",
                                   "kind": "randomizer", "tools": "tools/item-enemy-randomizer"},
    })

    with pytest.raises(ErmError) as excinfo:
        cli.cmd_apply(_apply_args("gameplay-extras-like"))
    capsys.readouterr()

    msg = str(excinfo.value)
    assert "gameplay-extras-like" in msg
    assert "randomizer-like" in msg
    assert "item-enemy-randomizer" in msg
    state = json.loads((tmp_path / "installed.json").read_text())
    assert list(state.keys()) == ["item-enemy-randomizer"]   # unchanged


def test_apply_unknown_profile_raises_patherror_not_filenotfound(
        tmp_path, monkeypatch, tmp_game):
    # A typo'd profile name must surface as a clean ErmError-derived PathError,
    # not a raw FileNotFoundError leaking from manifest.load_profile — same
    # contract fetch_profile already honors.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()

    with pytest.raises(PathError):
        cli.cmd_apply(_apply_args("no-such-profile-xyz"))


def test_apply_randomizer_extracts_to_tools_and_prints_proton_command(
        tmp_path, monkeypatch, capsys, tmp_game):
    # install="randomizer" is a special handler: the generator is a Windows
    # .exe the player has to run themselves under Proton to produce
    # regulation.bin — erm can't do that step for them, only extract the
    # generator and hand back the exact command to run it.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    # find_compatdata needs a real compatdata/<APPID> dir under the (fake)
    # steam root, which here is tmp_path itself.
    (tmp_path / "steamapps" / "compatdata" / paths.APPID).mkdir(parents=True)
    fake_proton = tmp_path / "compatibilitytools.d" / "GE-Proton10-31" / "proton"
    fake_proton.parent.mkdir(parents=True)
    fake_proton.write_bytes(b"\x00")
    monkeypatch.setattr(paths, "find_proton", lambda: fake_proton)

    _write_profile(tmp_path / "profiles", "randomizer-only",
        '[[mods]]\n'
        'id = "item-enemy-randomizer"\n'
        'source = "nexus"\n'
        'nexus_id = 428\n'
        'kind = "randomizer"\n'
        'install = "randomizer"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml",
               {"item-enemy-randomizer": ("1.0", "randomizer.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "randomizer.zip", "randomizer/EldenRingRandomizer.exe")

    rc = cli.cmd_apply(_apply_args("randomizer-only"))
    out = capsys.readouterr().out

    assert rc == 0
    extracted = (tmp_path / "tools" / "item-enemy-randomizer"
                 / "randomizer" / "EldenRingRandomizer.exe")
    assert extracted.exists()
    assert "EldenRingRandomizer.exe" in out
    assert str(fake_proton) in out          # the printed Proton run command
    assert "run" in out

    state = json.loads((tmp_path / "installed.json").read_text())
    entry = state["item-enemy-randomizer"]        # recorded so the exclude guard sees it
    assert entry["kind"] == "randomizer"
    assert entry["tools"] == str(Path("tools") / "item-enemy-randomizer")


def test_apply_randomizer_falls_back_when_no_proton_found(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "steamapps" / "compatdata" / paths.APPID).mkdir(parents=True)
    monkeypatch.setattr(paths, "find_proton", lambda: None)

    _write_profile(tmp_path / "profiles", "randomizer-only",
        '[[mods]]\n'
        'id = "item-enemy-randomizer"\n'
        'source = "nexus"\n'
        'nexus_id = 428\n'
        'kind = "randomizer"\n'
        'install = "randomizer"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml",
               {"item-enemy-randomizer": ("1.0", "randomizer.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "randomizer.zip", "randomizer/EldenRingRandomizer.exe")

    rc = cli.cmd_apply(_apply_args("randomizer-only"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "EldenRingRandomizer.exe" in out
    assert "no Proton" in out or "Proton/Wine" in out   # fallback note, no crash


def test_apply_me3_extracts_scaffolds_profile_and_not_recorded(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    # me3 is kind="loader" -> apply now auto-hardens; mock set_immutable so
    # this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)

    _write_profile(tmp_path / "profiles", "me3-only",
        '[[mods]]\n'
        'id = "me3"\n'
        'source = "github"\n'
        'repo_id = 540883721\n'
        'kind = "loader"\n'
        'install = "me3"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"me3": ("v1.0", "me3.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "me3.zip", "bin/me3.exe")

    rc = cli.cmd_apply(_apply_args("me3-only"))
    out = capsys.readouterr().out

    assert rc == 0
    assert (tmp_path / "tools" / "me3" / "bin" / "me3.exe").exists()
    prof = tmp_path / "tools" / "me3" / "erm-coop.me3"
    assert prof.exists()
    assert "me3.help" in out

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "me3" not in state           # a loader/tool, not a Game/ mod


def test_apply_me3_reconcile_write_failure_warns_and_preserves_earlier_state(
        tmp_path, monkeypatch, capsys, tmp_game):
    # me3profile.reconcile() runs AFTER write_state, regenerating
    # tools/me3/erm-coop.me3 on every apply. If that write fails (read-only
    # tools/, disk full, TOCTOU) it must not propagate out of cmd_apply — that
    # would be surprising given installed.json was already written safely;
    # warn and keep going instead.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    # me3 is kind="loader" -> apply now auto-hardens; mock set_immutable so
    # this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)

    _write_profile(tmp_path / "profiles", "game-then-me3",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "game"\n'
        '\n'
        '[[mods]]\n'
        'id = "me3"\n'
        'source = "github"\n'
        'repo_id = 2\n'
        'kind = "loader"\n'
        'install = "me3"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "me3": ("1.0", "me3.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")
    _zip_with(vendor / "me3.zip", "bin/me3.exe")

    # Fail only the scaffold write, so write_state (installed.json) still works
    # — that's what lets us prove mod-a's record survived.
    real_write_text = Path.write_text

    def boom(self, *a, **k):
        if self.name == "erm-coop.me3":
            raise OSError("read-only file system")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)

    rc = cli.cmd_apply(_apply_args("game-then-me3"))   # must NOT raise
    out = capsys.readouterr().out

    assert rc == 0
    assert (game_dir / "a.dll").exists()                # mod-a really installed
    assert (tmp_path / "tools" / "me3" / "bin" / "me3.exe").exists()  # me3 extracted
    assert "could not regenerate the me3 profile" in out.lower()   # warned, not crashed

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" in state          # earlier mod's state survived the failure
    assert "me3" not in state        # me3 is a tool, never recorded


def test_uninstall_profile_removes_recorded_randomizer_and_skips_unrecorded_me3(
        tmp_path, monkeypatch, capsys, tmp_game):
    # me3 installs to tools/ and is never recorded, so profile-uninstall must
    # skip it — not fall into the single-mod vendor-archive fallback, which would
    # open its tool zip and report a bogus "removed 0 file(s)". The randomizer
    # also lives in tools/ but IS recorded now (kind="randomizer"), so uninstall
    # removes its generator dir and forgets it.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.setattr(paths, "find_proton", lambda: None)
    monkeypatch.chdir(tmp_path)
    # me3 is kind="loader" -> apply now auto-hardens; mock set_immutable so
    # this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)
    (tmp_path / "steamapps" / "compatdata" / paths.APPID).mkdir(parents=True)

    _write_profile(tmp_path / "profiles", "tools-mix",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "game"\n'
        '\n'
        '[[mods]]\n'
        'id = "me3"\n'
        'source = "github"\n'
        'repo_id = 2\n'
        'kind = "loader"\n'
        'install = "me3"\n'
        '\n'
        '[[mods]]\n'
        'id = "item-enemy-randomizer"\n'
        'source = "nexus"\n'
        'nexus_id = 428\n'
        'kind = "randomizer"\n'
        'install = "randomizer"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "me3": ("1.0", "me3.zip"),
        "item-enemy-randomizer": ("1.0", "randomizer.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")
    _zip_with(vendor / "me3.zip", "bin/me3.exe")
    _zip_with(vendor / "randomizer.zip", "randomizer/EldenRingRandomizer.exe")

    cli.cmd_apply(_apply_args("tools-mix"))
    capsys.readouterr()

    # mod-a landed in Game/; the randomizer is recorded (tools/); me3 is not.
    state = json.loads((tmp_path / "installed.json").read_text())
    assert set(state.keys()) == {"mod-a", "item-enemy-randomizer"}
    assert (tmp_path / "tools" / "item-enemy-randomizer").is_dir()

    rc = cli.cmd_uninstall(_uninstall_args("tools-mix"))
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "a.dll").exists()            # mod-a really removed
    assert not (tmp_path / "tools" / "item-enemy-randomizer").exists()  # generator removed
    assert (tmp_path / "tools" / "me3").is_dir()        # me3 skipped, left on disk
    assert "randomizer generator" in out                # the recorded randomizer was removed
    # me3 is unrecorded: skipped, not force-read from its vendor archive.
    assert "for me3" not in out
    assert "vendor archive" not in out
    assert "removed 0 file" not in out

    after = json.loads((tmp_path / "installed.json").read_text())
    assert after == {}


def test_apply_me3_reconcile_preserves_user_additions_in_profile(
        tmp_path, monkeypatch, capsys, tmp_game):
    # reconcile() regenerates everything ABOVE the USER_MARKER on every apply
    # (it's derived from install state, not hand-mutated), but a player's own
    # additions BELOW the marker (their ersc.dll native, a randomizer output
    # package) must survive a re-apply.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    # me3 is kind="loader" -> apply now auto-hardens; mock set_immutable so
    # this never shells out to real sudo.
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: None)

    _write_profile(tmp_path / "profiles", "me3-only",
        '[[mods]]\n'
        'id = "me3"\n'
        'source = "github"\n'
        'repo_id = 540883721\n'
        'kind = "loader"\n'
        'install = "me3"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"me3": ("v1.0", "me3.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "me3.zip", "bin/me3.exe")

    # Seed a deliberately WRONG header above the marker: if apply merely left
    # an existing profile alone (the old scaffold's `if not prof.exists()`
    # behavior), this garbage header would still be sitting there afterward.
    # Only an actual reconcile() call regenerates it to the real header.
    prof_dir = tmp_path / "tools" / "me3"
    prof_dir.mkdir(parents=True)
    prof = prof_dir / "erm-coop.me3"
    prof.write_text(
        "STALE-GARBAGE-HEADER\n\n"
        + me3profile.USER_MARKER + "\n"
        '[[packages]]\nid = "my-hand-add"\npath = \'mods/mine/\'\n'
    )

    cli.cmd_apply(_apply_args("me3-only"))
    text = prof.read_text()

    assert "STALE-GARBAGE-HEADER" not in text     # header actually regenerated
    assert 'profileVersion = "v1"' in text         # ... to the real reconciled header
    assert "my-hand-add" in text                   # user region below the marker survives


def test_apply_me3_package_installs_and_reconciles(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A me3 content package (a loose asset-override archive) must land in
    # tools/me3/mods/<id>/, get recorded as kind="me3-package" (not a Game/
    # file list), and the regenerated erm-coop.me3 must list it.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "unit-cosmetic",
        '[[mods]]\n'
        'id = "unit-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 999\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"unit-mod": ("1.0", "unit-mod.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "unit-mod.zip", "w") as z:
        z.writestr("parts/wp_a.dcx", b"x")

    rc = cli.cmd_apply(_apply_args("unit-cosmetic"))
    out = capsys.readouterr().out

    assert rc == 0
    assert (tmp_path / "tools" / "me3" / "mods" / "unit-mod" / "parts").exists()
    assert "unit-mod" in out and "me3 package" in out

    state = json.loads((tmp_path / "installed.json").read_text())
    assert state["unit-mod"]["kind"] == "me3-package"
    assert state["unit-mod"]["package"] == str(Path("tools") / "me3" / "mods" / "unit-mod")

    prof_text = (tmp_path / "tools" / "me3" / "erm-coop.me3").read_text()
    assert "unit-mod" in prof_text


def test_apply_me3_package_with_regulation_warns_shared_mod(
        tmp_path, monkeypatch, capsys, tmp_game):
    # regulation.bin is a SHARED file (every co-op player needs the identical
    # one) — a me3 package that carries one is not a safe client-side install,
    # so apply must warn loudly instead of silently treating it as cosmetic.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "shared-cosmetic",
        '[[mods]]\n'
        'id = "shared-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 998\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"shared-mod": ("1.0", "shared-mod.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    with zipfile.ZipFile(vendor / "shared-mod.zip", "w") as z:
        z.writestr("regulation.bin", b"r")

    rc = cli.cmd_apply(_apply_args("shared-cosmetic"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "shared-mod" in out and "regulation.bin" in out and "SHARED" in out
    state = json.loads((tmp_path / "installed.json").read_text())
    assert state["shared-mod"]["kind"] == "me3-package"


def test_apply_me3_package_no_asset_root_warns_and_continues(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A me3-package archive that has no recognizable DVDBND asset tree can't
    # be installed automatically; it must warn and NOT sink mods installed
    # earlier in the same run (matches the existing per-mod isolation pattern).
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    _write_profile(tmp_path / "profiles", "mixed",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "test"\n'
        'install = "game"\n'
        '\n'
        '[[mods]]\n'
        'id = "weird-mod"\n'
        'source = "nexus"\n'
        'nexus_id = 997\n'
        'kind = "cosmetic"\n'
        'install = "me3-package"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {
        "mod-a": ("1.0", "mod-a.zip"),
        "weird-mod": ("1.0", "weird-mod.zip"),
    })
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")
    with zipfile.ZipFile(vendor / "weird-mod.zip", "w") as z:
        z.writestr("docs/notes.txt", b"n")

    rc = cli.cmd_apply(_apply_args("mixed"))
    out = capsys.readouterr().out

    assert rc == 0
    assert (game_dir / "a.dll").exists()
    assert "weird-mod" in out

    state = json.loads((tmp_path / "installed.json").read_text())
    assert "mod-a" in state
    assert "weird-mod" not in state


def test_switch_survives_bad_state_entry_and_ends_consistent(
        tmp_path, monkeypatch, capsys, tmp_game):
    # installed.json carries a normal entry plus a broken one (empty files, no
    # vendor archive to derive from). The broken entry makes _uninstall_one
    # raise PathError; switch must warn, keep going, apply the target, and leave
    # installed.json holding ONLY the target's mods — no stale entry, no crash.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

    (game_dir / "g.dll").write_bytes(b"\x00")
    (tmp_path / "installed.json").write_text(json.dumps({
        "good-mod": {"version": "1.0", "archive": "g.zip", "files": ["g.dll"]},
        "bad-mod": {"version": "1.0", "archive": "b.zip", "files": []},
    }))

    _write_profile(tmp_path / "profiles", "profile-b",
        '[[mods]]\n'
        'id = "mod-c"\n'
        'source = "github"\n'
        'repo_id = 3\n'
        'kind = "test"\n'
        'install = "game"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"mod-c": ("1.0", "mod-c.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-c.zip", "c.dll")

    rc = cli.cmd_switch(type("A", (), {"profile": "profile-b", "json": False})())
    out = capsys.readouterr().out

    assert rc == 0
    assert not (game_dir / "g.dll").exists()          # good-mod uninstalled
    assert (game_dir / "c.dll").exists()               # target profile applied
    assert "bad-mod" in out                            # warned about the broken entry
    after = json.loads((tmp_path / "installed.json").read_text())
    assert list(after.keys()) == ["mod-c"]             # no stale entry lingers


# --- auto-harden on apply/switch ---

_LOADER_PLUS_GAME_MOD = (
    '[[mods]]\n'
    'id = "mod-a"\n'
    'source = "github"\n'
    'repo_id = 1\n'
    'kind = "test"\n'
    'install = "game"\n'
    '\n'
    '[[mods]]\n'
    'id = "elden-mod-loader"\n'
    'source = "nexus"\n'
    'nexus_id = 117\n'
    'kind = "loader"\n'
    'install = "manual"\n'
)


def _seed_loader_profile(tmp_path, name="loader-profile"):
    _write_profile(tmp_path / "profiles", name, _LOADER_PLUS_GAME_MOD)
    _seed_lock(tmp_path / "mods.lock.toml", {"mod-a": ("1.0", "mod-a.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")


def test_apply_loader_profile_auto_hardens(tmp_path, monkeypatch, capsys, tmp_game):
    # A profile that loads mods via a proxy DLL (elden-mod-loader, kind="loader")
    # must trigger harden automatically, so an accidental vanilla "Play" click
    # can't fire EAC on the injected DLL.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_loader_profile(tmp_path)

    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))

    rc = cli.cmd_apply(_apply_args("loader-profile"))
    out = capsys.readouterr().out

    assert rc == 0
    spg = game_dir / "start_protected_game.exe"
    backup = game_dir / "start_protected_game.exe.erm-backup"
    assert backup.exists()                                          # real EAC launcher preserved
    assert spg.read_bytes() == (game_dir / "eldenring.exe").read_bytes()  # swapped
    assert calls == [(spg, True)]                                   # set_immutable called (mocked)
    assert "harden" in out.lower()
    assert "hardened" in out.lower()      # doctor now reports the hardened state as safe


def test_apply_no_loader_profile_does_not_harden(tmp_path, monkeypatch, capsys, tmp_game):
    # seamless-only-like: no loader mod at all -> ERSC can't auto-load a proxy
    # DLL, so there's nothing for harden to protect against. Must not trigger.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path / "profiles", "no-loader",
        '[[mods]]\n'
        'id = "mod-a"\n'
        'source = "github"\n'
        'repo_id = 1\n'
        'kind = "coop-framework"\n'
        'install = "game"\n'
    )
    _seed_lock(tmp_path / "mods.lock.toml", {"mod-a": ("1.0", "mod-a.zip")})
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    _zip_with(vendor / "mod-a.zip", "a.dll")

    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))

    rc = cli.cmd_apply(_apply_args("no-loader"))
    capsys.readouterr()

    assert rc == 0
    assert calls == []
    assert not (game_dir / "start_protected_game.exe.erm-backup").exists()
    assert not harden.is_hardened(game_dir)


def test_apply_no_harden_flag_skips(tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_loader_profile(tmp_path)

    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))

    args = type("A", (), {"profile": "loader-profile", "json": False, "no_harden": True})()
    rc = cli.cmd_apply(args)
    capsys.readouterr()

    assert rc == 0
    assert calls == []
    assert not harden.is_hardened(game_dir)


def test_apply_already_hardened_does_not_reharden(tmp_path, monkeypatch, capsys, tmp_game):
    # If the real EAC backup already exists (from an earlier `erm harden` or a
    # prior auto-harden), apply must not re-run harden_swap -- that's the exact
    # bug that would let a second backup overwrite the real EAC launcher with
    # the eldenring copy, destroying the only copy of the real launcher.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_loader_profile(tmp_path)

    harden.harden_swap(game_dir)   # pre-harden
    backup = game_dir / "start_protected_game.exe.erm-backup"
    backup_bytes = backup.read_bytes()

    calls = []
    monkeypatch.setattr(harden, "set_immutable", lambda path, on: calls.append((path, on)))

    rc = cli.cmd_apply(_apply_args("loader-profile"))
    capsys.readouterr()

    assert rc == 0
    assert backup.read_bytes() == backup_bytes    # the real EAC backup untouched
    assert calls == []                            # is_hardened guard: apply doesn't touch it


def test_apply_auto_harden_sudo_failure_warns_not_crash(tmp_path, monkeypatch, capsys, tmp_game):
    # set_immutable raising (e.g. sudo declined) must warn and keep going --
    # the mods are already installed; harden is a safety add-on, not a gate.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _seed_loader_profile(tmp_path)

    def _boom(path, on):
        raise ErmError("chattr +i failed (rc 1) — sudo declined")
    monkeypatch.setattr(harden, "set_immutable", _boom)

    rc = cli.cmd_apply(_apply_args("loader-profile"))   # must NOT raise
    out = capsys.readouterr().out

    assert rc == 0
    assert "auto-harden incomplete" in out.lower()
    assert "safety check" in out.lower()   # final doctor block still ran
    assert "traceback" not in out.lower()
