"""Integration matrix: me3 profile + launch option are order-independent.

The core design guarantee is "derive, don't mutate" — installed.json is the
only mutated state, and both tools/me3/erm-coop.me3 (via me3profile.reconcile)
and the me3-launch-mode flag (state_mod.has_me3_packages) are pure functions
of that state. So any sequence of apply/uninstall/switch that ends up at the
same install state must produce byte-identical output, regardless of the
order operations ran in. These tests build two synthetic profiles (a coop
profile installing seamless-coop via install="game", and a cosmetics profile
installing a me3-package cosmetic) and drive the real cli.cmd_apply /
cmd_uninstall / cmd_switch across every combination in the matrix.
"""
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ermlib import cli, paths
from ermlib import state as state_mod

ME3_PROFILE = Path("tools") / "me3" / "erm-coop.me3"


def _make_ersc_zip(path):
    # Same shape as the real Seamless Co-op release archive, mirroring the
    # _make_ersc_zip helper in test_cli_uninstall.py.
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def _write_profile(profiles_dir, name, mods_toml):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "integration matrix fixture profile"\n'
        '\n' + mods_toml
    )


# kind="coop-framework" -> no auto-harden (only kind="loader" triggers that).
_COOP_PROFILE = (
    '[[mods]]\n'
    'id = "seamless-coop"\n'
    'source = "nexus"\n'
    'nexus_id = 510\n'
    'kind = "coop-framework"\n'
    'install = "game"\n'
)

# kind="cosmetic" + install="me3-package" -> no auto-harden either; erm
# extracts it as a me3 content package under tools/me3/mods/<id>/.
_COSMETICS_PROFILE = (
    '[[mods]]\n'
    'id = "cosmetic-mod"\n'
    'source = "nexus"\n'
    'nexus_id = 2431\n'
    'kind = "cosmetic"\n'
    'install = "me3-package"\n'
)


def _seed_lock(tmp_path):
    (tmp_path / "mods.lock.toml").write_text(
        '[seamless-coop]\n'
        'version = "1.9.9"\n'
        'asset = "seamless-coop.zip"\n'
        'sha256 = "a"\n'
        'source = "nexus"\n'
        '\n'
        '[cosmetic-mod]\n'
        'version = "1.0"\n'
        'asset = "cosmetic-mod.zip"\n'
        'sha256 = "b"\n'
        'source = "nexus"\n'
    )


def _seed_vendor(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir(exist_ok=True)
    _make_ersc_zip(vendor / "seamless-coop.zip")
    # A real DVDBND asset tree (parts/) so me3pkg.find_package_root resolves it.
    with zipfile.ZipFile(vendor / "cosmetic-mod.zip", "w") as z:
        z.writestr("parts/wp_a.dcx", b"x")


def _reset_game_dir(game_dir):
    """Rebuild the fake Game/ dir from scratch at the SAME absolute path every
    time. Keeping the path fixed across _final() calls in a test matters: the
    me3 profile's [[natives]] entry embeds game_dir/SeamlessCoop/ersc.dll's
    *resolved* absolute path, so two _final() calls being compared for exact
    text equality must share that path or the comparison is meaningless."""
    if game_dir.exists():
        shutil.rmtree(game_dir)
    game_dir.mkdir(parents=True)
    (game_dir / "eldenring.exe").write_bytes(b"\x00")
    (game_dir / "start_protected_game.exe").write_bytes(b"\x00" * 16)
    sc = game_dir / "SeamlessCoop"
    sc.mkdir()
    (sc / "ersc.dll").write_bytes(b"\x00")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """One temp cwd, reused for every _final() call in a test: same profiles/,
    vendor/, mods.lock.toml, and (critically) the same absolute game_dir path."""
    game_dir = tmp_path / "Game"
    monkeypatch.setattr(paths, "find_steam_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "find_game_dir", lambda root: game_dir)
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path / "profiles", "coop", _COOP_PROFILE)
    _write_profile(tmp_path / "profiles", "cosmetics", _COSMETICS_PROFILE)
    _seed_lock(tmp_path)
    _seed_vendor(tmp_path)
    return SimpleNamespace(root=tmp_path, game_dir=game_dir)


def _apply(profile):
    return ("apply", profile)


def _uninstall(profile):
    return ("uninstall", profile)


def _switch(profile):
    return ("switch", profile)


def _run_op(op):
    action, profile = op
    args = type("A", (), {"profile": profile, "mod": profile, "json": False})()
    if action == "apply":
        return cli.cmd_apply(args)
    if action == "uninstall":
        return cli.cmd_uninstall(args)
    if action == "switch":
        return cli.cmd_switch(args)
    raise ValueError(f"unknown op {action!r}")


def _final(ws, order):
    """Reset installed.json/tools/Game to a pristine baseline (same cwd, same
    game_dir path), run `order` (a list of _apply/_uninstall/_switch tuples)
    through the real cli commands, and return the final installed.json dict,
    the erm-coop.me3 text, and the me3-launch-mode flag."""
    _reset_game_dir(ws.game_dir)
    installed = ws.root / "installed.json"
    if installed.exists():
        installed.unlink()
    tools = ws.root / "tools"
    if tools.exists():
        shutil.rmtree(tools)

    for op in order:
        rc = _run_op(op)
        assert rc == 0, f"{op} returned rc={rc}"

    state = state_mod.load_state()
    profile_text = (ws.root / ME3_PROFILE).read_text()
    me3_mode = state_mod.has_me3_packages(state)
    return SimpleNamespace(state=state, profile_text=profile_text, me3_mode=me3_mode)


def test_apply_order_is_commutative(workspace):
    a = _final(workspace, [_apply("coop"), _apply("cosmetics")])
    b = _final(workspace, [_apply("cosmetics"), _apply("coop")])

    assert a.state == b.state
    assert a.profile_text == b.profile_text
    assert a.me3_mode is True
    assert b.me3_mode is True
    # both mods really landed: ersc native + the me3 package
    assert "[[natives]]" in a.profile_text
    assert "[[packages]]" in a.profile_text
    assert "seamless-coop" in a.state
    assert "cosmetic-mod" in a.state


def test_cosmetics_alone_has_no_ersc_native(workspace):
    r = _final(workspace, [_apply("cosmetics")])

    assert "[[natives]]" not in r.profile_text        # no Seamless installed
    assert "[[packages]]" in r.profile_text
    assert r.me3_mode is True
    assert "seamless-coop" not in r.state


def test_remove_cosmetics_reverts_to_ersc_mode(workspace):
    removed = _final(workspace, [
        _apply("coop"), _apply("cosmetics"), _uninstall("cosmetics"),
    ])

    assert removed.me3_mode is False
    assert "[[packages]]" not in removed.profile_text
    assert "[[natives]]" in removed.profile_text
    assert "seamless-coop" in removed.state
    assert "cosmetic-mod" not in removed.state


def test_remove_coop_keeps_packages_drops_native(workspace):
    r = _final(workspace, [
        _apply("coop"), _apply("cosmetics"), _uninstall("coop"),
    ])

    assert "[[packages]]" in r.profile_text
    assert "[[natives]]" not in r.profile_text
    assert r.me3_mode is True
    assert "cosmetic-mod" in r.state
    assert "seamless-coop" not in r.state


def test_switch_to_seamless_only_clears_packages(workspace):
    r = _final(workspace, [
        _apply("coop"), _apply("cosmetics"), _switch("coop"),
    ])

    assert "[[packages]]" not in r.profile_text
    assert r.me3_mode is False
    assert "cosmetic-mod" not in r.state
    # switching to "coop" re-applies it, so the native entry is back
    assert "[[natives]]" in r.profile_text
    assert "seamless-coop" in r.state


def test_double_apply_cosmetics_is_idempotent(workspace):
    once = _final(workspace, [_apply("coop"), _apply("cosmetics")])
    twice = _final(workspace, [_apply("coop"), _apply("cosmetics"), _apply("cosmetics")])

    assert once.state == twice.state
    assert once.profile_text == twice.profile_text
    assert once.me3_mode is twice.me3_mode is True
    # no duplicate [[packages]] block for the re-applied mod
    assert twice.profile_text.count("[[packages]]") == 1
