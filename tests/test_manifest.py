from pathlib import Path

import pytest

from ermlib.manifest import load_profile, write_lock, load_lock, set_mod
from ermlib.errors import PathError


def test_seamless_only_profile_has_ersc():
    prof = load_profile("seamless-only", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    assert "seamless-coop" in ids
    ersc = next(m for m in prof["mods"] if m["id"] == "seamless-coop")
    # Sourced from Nexus (#510), not the GitHub mirror (497113840) — the
    # mirror lags Nexus, see the comment in profiles/seamless-only.toml.
    assert ersc["source"] == "nexus"
    assert ersc["nexus_id"] == 510


def test_seamless_full_profile_loads_all_mods():
    prof = load_profile("seamless-full", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    # seamless-full composes seamless-extras (the base coop framework + loader +
    # a couple client-side QoL/cosmetics, defined once there) and gameplay-extras
    # (Clever's Moveset) — the single "what the whole party needs" profile.
    assert set(prof["includes"]) == {"seamless-extras", "gameplay-extras"}
    assert "clevers-moveset" in ids
    # base coop framework + loader come from the seamless-extras include now,
    # not redundant own entries
    assert "seamless-coop" in ids
    assert "elden-mod-loader" in ids

    ersc = next(m for m in prof["mods"] if m["id"] == "seamless-coop")
    assert ersc["source"] == "nexus"
    assert ersc["nexus_id"] == 510

    me3 = next(m for m in prof["mods"] if m["id"] == "me3")
    assert me3["source"] == "github"
    assert me3["repo_id"] == 540883721
    assert me3["asset_match"] == "me3-windows-amd64"
    assert me3["install"] == "me3"

    # item-enemy-randomizer is disabled — Clever's Moveset (gameplay-extras) owns
    # the single regulation.bin slot now.
    assert "item-enemy-randomizer" not in ids

    # pause-the-game is excluded — it can't work in a networked co-op session
    assert "pause-the-game" not in ids
    # eac-toggler is redundant with the launch-option method + `erm harden`,
    # and its own winhttp.dll is itself an artifact `erm doctor` flags
    assert "eac-toggler" not in ids
    # unlock-the-fps desyncs co-op physics above 60fps — not usable with Seamless
    assert "unlock-the-fps" not in ids

    assert len(ids) == len(set(ids))   # no duplicate mod ids


def test_single_full_profile_is_non_coop_with_the_conflict_mods():
    prof = load_profile("single-full", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    # single-player: no Seamless Co-op
    assert "seamless-coop" not in ids
    # the mods that desync co-op but work solo are included
    assert "pause-the-game" in ids
    assert "unlock-the-fps" in ids
    # me3 still routes to its special install kind
    me3 = next(m for m in prof["mods"] if m["id"] == "me3")
    assert me3["install"] == "me3"
    # randomizer disabled; Clever's Moveset (composed via includes) owns the regulation slot
    assert "item-enemy-randomizer" not in ids
    assert "clevers-moveset" in ids
    assert len(ids) == len(set(ids))   # no duplicate mod ids


def test_cosmetic_extras_is_a_separate_client_side_overlay():
    # cosmetic-extras is a per-machine, toggleable overlay applied ON TOP of a
    # base coop profile — not a standalone stack. Every mod is client-side
    # (visual only, no regulation.bin), so a co-op partner who doesn't run it
    # is unaffected — a lower-powered box (a Steam Deck, say) can skip it.
    prof = load_profile("cosmetic-extras", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    assert ids == ["texture-improvement", "weapons-animated-glow"]

    # No coop framework and no loader: it rides the base profile's me3, and with
    # no loader mod it must never trip apply's auto-harden on its own.
    assert "seamless-coop" not in ids
    assert not any(m.get("kind") == "loader" for m in prof["mods"])

    for m in prof["mods"]:
        assert m["kind"] == "cosmetic"
        assert m["source"] == "nexus"
        assert isinstance(m["nexus_id"], int)
        # These are me3-VFS asset overrides (loose texture/menu/sfx files), auto-installed
        # by erm's me3-package handler.
        assert m["install"] == "me3-package"

    by_id = {m["id"]: m for m in prof["mods"]}
    assert by_id["texture-improvement"]["nexus_id"] == 2431
    assert by_id["weapons-animated-glow"]["nexus_id"] == 4433

    # minimal-hud (#148) was dropped: its mid-2022 menu .gfx render as tofu on the
    # current post-DLC build. Left out until a version-compatible minimal HUD exists.
    assert "minimal-hud" not in ids
    assert by_id["texture-improvement"].get("subdir") is None
    assert by_id["weapons-animated-glow"].get("subdir") is None

    # De-conflicted: the alternative HUD (#6265) and weapon mod (#4307) override
    # the same files as the chosen ones, so they're not both present.
    assert "clean-hud" not in ids
    assert "golems-glow-arsenal" not in ids
    assert len(ids) == len(set(ids))


def test_gameplay_extras_is_a_shared_coop_overlay():
    # gameplay-extras is the OPPOSITE of cosmetic-extras: shared gameplay mods
    # that every player must run identically (requires_all_players), not
    # per-machine visuals. No loader → never trips auto-harden on its own.
    prof = load_profile("gameplay-extras", base=Path("profiles"))

    cl = next(m for m in prof["mods"] if m["id"] == "clevers-moveset")
    assert cl["nexus_id"] == 1928
    assert cl["file_id"] == 34558
    assert cl["install"] == "me3-package"
    assert cl["kind"] == "overhaul"
    assert cl["requires_all_players"] is True

    # Both removed: summon-anywhere (useless in co-op), boss-res (msg conflict w/ Clever's).
    ids = [m["id"] for m in prof["mods"]]
    assert "summon-anywhere" not in ids
    assert "boss-resurrection-lite" not in ids

    assert not any(m.get("kind") == "loader" for m in prof["mods"])


def test_seamless_randomizer_me3_uses_numeric_id():
    # me3's repo_id used to be a slug ("garyttierney/me3"), which the
    # numeric-id-only GitHub fetch (api.github.com/repositories/<id>/...)
    # can't resolve — it 404s. Must be the numeric repository id.
    prof = load_profile("seamless-randomizer", base=Path("profiles"))
    me3 = next(m for m in prof["mods"] if m["id"] == "me3")
    assert me3["repo_id"] == 540883721
    assert isinstance(me3["repo_id"], int)
    assert me3["asset_match"] == "me3-windows-amd64"


def test_lock_roundtrip(tmp_path):
    lock = {}
    set_mod(lock, "seamless-coop", version="v1.9.8", asset="Seamless.zip",
            sha256="1a956a30", source="github")
    p = tmp_path / "mods.lock.toml"
    write_lock(p, lock)
    back = load_lock(p)
    assert back["seamless-coop"]["version"] == "v1.9.8"
    assert back["seamless-coop"]["sha256"] == "1a956a30"


def test_lock_roundtrip_escapes_special_chars(tmp_path):
    lock = {}
    nasty = 'we"ird\\name\nx.zip'
    set_mod(lock, "seamless-coop", version="v1.9.8", asset=nasty,
            sha256="1a956a30", source="github")
    p = tmp_path / "mods.lock.toml"
    write_lock(p, lock)
    back = load_lock(p)
    assert back["seamless-coop"]["asset"] == nasty


def test_lock_deterministic_multi_mod_order(tmp_path):
    lock = {}
    set_mod(lock, "zebra", version="v1", asset="z.zip",
            sha256="ff", source="github")
    set_mod(lock, "alpha", version="v1", asset="a.zip",
            sha256="aa", source="github")
    p = tmp_path / "mods.lock.toml"
    write_lock(p, lock)
    text = p.read_text()
    assert text.index("[alpha]") < text.index("[zebra]")


def _mod(base, name, includes=None, mods=None, excludes=None):
    lines = [f'name = "{name}"']
    if includes:
        lines.append("includes = [" + ", ".join(f'"{i}"' for i in includes) + "]")
    if excludes:
        lines.append("excludes = [" + ", ".join(f'"{e}"' for e in excludes) + "]")
    for m in mods or []:
        lines += ["", "[[mods]]"] + [f'{k} = {v!r}' if not isinstance(v, str) else f'{k} = "{v}"'
                                     for k, v in m.items()]
    (base / f"{name}.toml").write_text("\n".join(lines) + "\n")


def test_profile_includes_composes_mods_included_first(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1}])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "b", "source": "nexus", "nexus_id": 2}])
    prof = load_profile("parent", base=tmp_path)
    assert [m["id"] for m in prof["mods"]] == ["a", "b"]   # included first, then own


def test_profile_includes_own_entry_overrides_included(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1, "install": "game"}])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "a", "source": "nexus", "nexus_id": 1, "install": "mods"}])
    prof = load_profile("parent", base=tmp_path)
    assert [m["id"] for m in prof["mods"]] == ["a"]        # deduped
    assert prof["mods"][0]["install"] == "mods"            # own entry wins


def test_profile_includes_cycle_raises(tmp_path):
    _mod(tmp_path, "x", includes=["y"])
    _mod(tmp_path, "y", includes=["x"])
    with pytest.raises(PathError):
        load_profile("x", base=tmp_path)


def test_profile_includes_unknown_raises(tmp_path):
    _mod(tmp_path, "p", includes=["nope-xyz"])
    with pytest.raises(PathError):
        load_profile("p", base=tmp_path)


def test_profile_includes_merges_excludes_from_included_profile(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1}],
         excludes=["z"])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "b", "source": "nexus", "nexus_id": 2}], excludes=["w"])
    prof = load_profile("parent", base=tmp_path)
    assert set(prof["excludes"]) == {"z", "w"}
    assert len(prof["excludes"]) == len(set(prof["excludes"]))   # deduped


def test_randomizer_profile_excludes_gameplay_extras():
    prof = load_profile("randomizer", base=Path("profiles"))
    assert prof["excludes"] == ["gameplay-extras"]
    ids = [m["id"] for m in prof["mods"]]
    assert ids == ["item-enemy-randomizer"]


def test_gameplay_extras_excludes_randomizer():
    prof = load_profile("gameplay-extras", base=Path("profiles"))
    assert "randomizer" in prof["excludes"]


def test_seamless_full_excludes_randomizer_via_gameplay_extras_include():
    # seamless-full doesn't declare its own excludes — it inherits gameplay-extras'
    # via the includes merge, since it composes gameplay-extras (Clever's Moveset).
    prof = load_profile("seamless-full", base=Path("profiles"))
    assert "randomizer" in prof["excludes"]


def test_experimental_composes_seamless_full_with_the_trial_overlays():
    prof = load_profile("experimental", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    assert len(ids) == len(set(ids))   # no duplicate mod ids
    # Composes the coop stack rather than replacing it — `erm switch` uninstalls
    # everything first, so a standalone profile would strip Seamless.
    assert prof["includes"] == ["seamless-full"]
    assert "seamless-coop" in ids and "clevers-moveset" in ids
    # Nothing on trial right now — both candidates were rejected. questpath's
    # render hooks killed the game at startup; map-for-goblins ran fine but its
    # overlay takes no controller input.
    assert "questpath" not in ids
    assert "map-for-goblins" not in ids
    # Inherits seamless-full's mutual exclusion with the randomizer.
    assert "randomizer" in prof["excludes"]


def test_merges_and_prunes_resolve_through_includes(tmp_path):
    """A merge declared in an included profile is inherited, the same way
    excludes are — otherwise every composing profile would have to repeat it."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[merges]]\n'
        'path = "msg/engus/menu_dlc02.msgbnd.dcx"\n'
        'strategy = "fmg-union"\n'
        'mods = ["a", "b"]\n'
        'prefer = "a"\n'
        '[[prunes]]\n'
        'mod = "b"\n'
        'paths = ["msg/engus/item_dlc02.msgbnd.dcx"]\n')
    (tmp_path / "top.toml").write_text('name = "top"\nincludes = ["base"]\n')

    prof = load_profile("top", base=tmp_path)
    assert [m["path"] for m in prof["merges"]] == ["msg/engus/menu_dlc02.msgbnd.dcx"]
    assert prof["merges"][0]["prefer"] == "a"
    assert prof["prunes"][0]["paths"] == ["msg/engus/item_dlc02.msgbnd.dcx"]


def test_a_merge_declared_twice_is_deduplicated(tmp_path):
    """Two profiles in the include graph may both declare the same merge.
    Applying it twice would merge an already-merged file into itself."""
    body = ('[[merges]]\n'
            'path = "p"\nstrategy = "fmg-union"\nmods = ["a", "b"]\nprefer = "a"\n')
    (tmp_path / "one.toml").write_text('name = "one"\n' + body)
    (tmp_path / "two.toml").write_text('name = "two"\nincludes = ["one"]\n' + body)

    prof = load_profile("two", base=tmp_path)
    assert len(prof["merges"]) == 1


def test_profiles_without_merges_get_empty_lists(tmp_path):
    (tmp_path / "plain.toml").write_text('name = "plain"\n')
    prof = load_profile("plain", base=tmp_path)
    assert prof["merges"] == []
    assert prof["prunes"] == []

