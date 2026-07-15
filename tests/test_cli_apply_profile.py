import json
import shutil
import zipfile
from pathlib import Path

import pytest

from ermlib import cli, paths
from ermlib.errors import PathError
from tests.conftest import REPO


def _write_profile(profiles_dir, name, mods_toml):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test fixture profile"\n'
        '\n' + mods_toml
    )


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


def test_apply_two_mod_profile_installs_game_and_mods_targets_and_skips_manual(
        tmp_path, monkeypatch, capsys, tmp_game):
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
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
    assert "item-enemy-randomizer" not in state   # a tool, not a Game/ mod


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


def test_apply_me3_does_not_clobber_existing_profile(
        tmp_path, monkeypatch, capsys, tmp_game):
    # A player edits tools/me3/erm-coop.me3 by hand (pointing it at their
    # ersc.dll + randomizer output); re-running apply must leave it alone.
    game_dir = tmp_game
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)

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

    prof_dir = tmp_path / "tools" / "me3"
    prof_dir.mkdir(parents=True)
    prof = prof_dir / "erm-coop.me3"
    prof.write_text("# hand-edited by the player\n")

    cli.cmd_apply(_apply_args("me3-only"))

    assert prof.read_text() == "# hand-edited by the player\n"


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
