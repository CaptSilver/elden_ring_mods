import shutil
import warnings
import zipfile
from pathlib import Path

import pytest

from ermlib import install
from ermlib.manifest import load_profile, write_lock, load_lock, set_mod
from ermlib.errors import PathError


def _raise_bad_zip(*args, **kwargs):
    raise zipfile.BadZipFile("unreadable archive")


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

    # The loader Steam actually runs is the native Linux build; its Windows
    # components ride along in the same tarball.
    me3 = next(m for m in prof["mods"] if m["id"] == "me3-host")
    assert me3["source"] == "github"
    assert me3["repo_id"] == 540883721
    assert me3["asset_match"] == "me3-linux-amd64"
    assert me3["install"] == "me3-host"

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
    me3 = next(m for m in prof["mods"] if m["id"] == "me3-host")
    assert me3["install"] == "me3-host"
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

    br = next(m for m in prof["mods"] if m["id"] == "boss-resurrection-lite")
    assert br["nexus_id"] == 2790
    assert br["file_id"] == 24925          # the Lite build (no regulation.bin)
    assert br["install"] == "me3-package"
    assert br["requires_all_players"] is True

    # summon-anywhere was removed (loaded fine, but useless in co-op).
    assert "summon-anywhere" not in [m["id"] for m in prof["mods"]]

    assert not any(m.get("kind") == "loader" for m in prof["mods"])


def test_boss_resurrection_conflict_is_declared():
    """Boss Res and Clever's both ship menu_dlc02, and me3 mounts one file per
    path. Without the merge declaration one of them silently isn't in the game."""
    prof = load_profile("gameplay-extras", base=Path("profiles"))
    merge = next(m for m in prof["merges"]
                 if m["path"] == "msg/engus/menu_dlc02.msgbnd.dcx")
    assert merge["strategy"] == "fmg-union"
    assert set(merge["mods"]) == {"clevers-moveset", "boss-resurrection-lite"}
    assert merge["prefer"] == "clevers-moveset"

    # Boss Res also ships item_dlc02 and systemparam byte-identical to its own
    # baseline; its item_dlc02 is older vanilla that would regress item text.
    prune = next(p for p in prof["prunes"] if p["mod"] == "boss-resurrection-lite")
    assert "msg/engus/item_dlc02.msgbnd.dcx" in prune["paths"]
    assert "param/systemparam/systemparam.parambnd.dcx" in prune["paths"]


def test_seamless_randomizer_me3_uses_numeric_id():
    # me3's repo_id used to be a slug ("garyttierney/me3"), which the
    # numeric-id-only GitHub fetch (api.github.com/repositories/<id>/...)
    # can't resolve — it 404s. Must be the numeric repository id.
    prof = load_profile("seamless-randomizer", base=Path("profiles"))
    me3 = next(m for m in prof["mods"] if m["id"] == "me3-host")
    assert me3["repo_id"] == 540883721
    assert isinstance(me3["repo_id"], int)
    assert me3["asset_match"] == "me3-linux-amd64"


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
    mods = {m["id"]: m for m in prof["mods"]}
    ids = list(mods)
    assert len(ids) == len([m["id"] for m in prof["mods"]])   # no duplicate mod ids
    # Composes the coop stack rather than replacing it — `erm switch` uninstalls
    # everything first, so a standalone profile would strip Seamless.
    assert prof["includes"] == ["seamless-full"]
    assert "seamless-coop" in ids and "clevers-moveset" in ids
    # The overlays this profile exists to stage before they reach the real stack.
    assert "map-for-goblins" in ids
    # map-for-goblins MUST stay me3-native, never "mods": under EML's load_delay
    # the dll injects after the world map is built and no icons appear. And the
    # file_id must stay pinned — the mod ships nine MAIN variants, one per
    # overhaul, so an unpinned fetch would grab the wrong one.
    assert mods["map-for-goblins"]["install"] == "me3-native"
    assert mods["map-for-goblins"]["file_id"] == 48311
    # questpath stays rejected — its render hooks killed the game at startup here.
    assert "questpath" not in ids
    # starlight-shards-rune-arcs stays rejected — it loads through either loader
    # and no-ops, because its AOB scan doesn't match this game build. Re-adding it
    # costs a launch to rediscover that, so the profile has to keep it out.
    assert "starlight-shards-rune-arcs" not in ids
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


def test_renames_resolve_through_includes(tmp_path):
    """A rename is packaging metadata like a prune, so it has to travel with the
    mod through the include chain. Declared ONLY in the included profile, so the
    raw TOML key on the top profile can't make this pass on its own."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[renames]]\n'
        'mod = "b"\n'
        'paths = { "msg/engUS/item.dcx" = "msg/engus/item.dcx" }\n')
    (tmp_path / "top.toml").write_text('name = "top"\nincludes = ["base"]\n')

    prof = load_profile("top", base=tmp_path)
    assert len(prof["renames"]) == 1
    assert prof["renames"][0]["paths"] == {"msg/engUS/item.dcx": "msg/engus/item.dcx"}


def test_a_rename_declared_twice_is_deduplicated(tmp_path):
    """Running one twice would raise the second time round: the source has
    already moved, and its destination is now occupied by the mod's own file."""
    body = ('[[renames]]\n'
            'mod = "b"\n'
            'paths = { "msg/engUS/item.dcx" = "msg/engus/item.dcx" }\n')
    (tmp_path / "one.toml").write_text('name = "one"\n' + body)
    (tmp_path / "two.toml").write_text('name = "two"\nincludes = ["one"]\n' + body)

    assert len(load_profile("two", base=tmp_path)["renames"]) == 1


def test_profiles_without_renames_get_an_empty_list(tmp_path):
    (tmp_path / "bare.toml").write_text('name = "bare"\n')
    assert load_profile("bare", base=tmp_path)["renames"] == []


def test_a_merge_declared_twice_is_deduplicated(tmp_path):
    """Two profiles in the include graph may both declare the same merge.
    Applying it twice would merge an already-merged file into itself."""
    body = ('[[merges]]\n'
            'path = "p"\nstrategy = "fmg-union"\nmods = ["a", "b"]\nprefer = "a"\n')
    (tmp_path / "one.toml").write_text('name = "one"\n' + body)
    (tmp_path / "two.toml").write_text('name = "two"\nincludes = ["one"]\n' + body)

    prof = load_profile("two", base=tmp_path)
    assert len(prof["merges"]) == 1


def test_a_merge_declared_twice_with_different_prefer_is_not_deduplicated(tmp_path):
    """Two profiles in an include chain can name the same path and mods but
    disagree on `prefer` (or strategy). Deduping on (path, mods) alone would
    silently keep whichever loaded first and drop the other's `prefer` --
    exactly the kind of silent override load_profile's own docstring promises
    LATEST wins for. Both entries must survive so conflicts._declare_merges
    gets the chance to raise on the disagreement."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[merges]]\n'
        'path = "msg/x.dcx"\nstrategy = "fmg-union"\nmods = ["mod-x", "mod-y"]\n'
        'prefer = "mod-x"\n')
    (tmp_path / "top.toml").write_text(
        'name = "top"\nincludes = ["base"]\n'
        '[[merges]]\n'
        'path = "msg/x.dcx"\nstrategy = "fmg-union"\nmods = ["mod-x", "mod-y"]\n'
        'prefer = "mod-y"\n')

    prof = load_profile("top", base=tmp_path)
    assert len(prof["merges"]) == 2
    assert {m["prefer"] for m in prof["merges"]} == {"mod-x", "mod-y"}


def test_profiles_without_merges_get_empty_lists(tmp_path):
    (tmp_path / "plain.toml").write_text('name = "plain"\n')
    prof = load_profile("plain", base=tmp_path)
    assert prof["merges"] == []
    assert prof["prunes"] == []



def test_a_merge_entry_with_a_nested_table_is_deduplicated(tmp_path):
    """Three-way strategies declare `vanilla = { mod = ..., member = ... }`, a
    nested TOML table. The dedup key handled lists but not dicts, so composing
    two profiles that both declared such a merge crashed before any of the
    disagreement checks could run."""
    base = tmp_path / "profiles"
    base.mkdir()
    merge_toml = (
        '[[merges]]\n'
        'path = "regulation.bin"\n'
        'strategy = "param-rows"\n'
        'mods = ["a", "b"]\n'
        'prefer = "a"\n'
        'vanilla = { mod = "rando", member = "Vanilla/regulation.bin" }\n'
    )
    (base / "leaf.toml").write_text('name = "leaf"\ndescription = "d"\n\n' + merge_toml)
    (base / "top.toml").write_text(
        'name = "top"\ndescription = "d"\nincludes = ["leaf"]\n\n' + merge_toml)

    prof = load_profile("top", base=base)
    assert len(prof["merges"]) == 1
    assert prof["merges"][0]["vanilla"]["member"] == "Vanilla/regulation.bin"


def test_nested_tables_that_differ_are_kept_as_separate_entries(tmp_path):
    # Two declarations for the same path that disagree must both survive here,
    # so conflicts._declare_merges can refuse them by name rather than one
    # being silently dropped at load time.
    base = tmp_path / "profiles"
    base.mkdir()
    def merge_for(member):
        return ('[[merges]]\n'
                'path = "regulation.bin"\n'
                'strategy = "param-rows"\n'
                'mods = ["a", "b"]\n'
                'prefer = "a"\n'
                f'vanilla = {{ mod = "rando", member = "{member}" }}\n')
    (base / "leaf.toml").write_text('name = "leaf"\ndescription = "d"\n\n' + merge_for("one.bin"))
    (base / "top.toml").write_text(
        'name = "top"\ndescription = "d"\nincludes = ["leaf"]\n\n' + merge_for("two.bin"))
    assert len(load_profile("top", base=base)["merges"]) == 2


def test_nofalldead_is_a_shared_mod_with_its_merges():
    """Promoted out of experimental once the in-game check passed: the talisman
    really does stop fall death. It edits regulation.bin, so every player in the
    party needs the identical merged file -- which is why it belongs in
    gameplay-extras with requires_all_players rather than a per-machine overlay."""
    shared = load_profile("gameplay-extras", base=Path("profiles"))
    nfd = next(m for m in shared["mods"] if m["id"] == "nofalldead")
    assert nfd["requires_all_players"] is True
    assert nfd["install"] == "me3-package"
    # The mod ships two MAIN files; 48339 is the always-on variant, not this one.
    assert nfd["file_id"] == 48340

    # The merges have to live with it: without them two packages both ship
    # regulation.bin and the apply aborts on an undeclared collision.
    merges = {m["path"]: m for m in shared["merges"]}
    assert merges["regulation.bin"]["strategy"] == "param-rows"
    assert merges["msg/engus/item_dlc02.msgbnd.dcx"]["strategy"] == "fmg-3way"
    for path in ("regulation.bin", "msg/engus/item_dlc02.msgbnd.dcx"):
        assert merges[path]["prefer"] == "clevers-moveset"
        assert merges[path]["vanilla"]["mod"] == "item-enemy-randomizer"


def test_map_for_goblins_is_client_side_in_both_full_profiles():
    """Client-side overlay, so it is duplicated into the two standalone profiles
    rather than put in gameplay-extras -- a partner who doesn't run it is
    unaffected, and single-full doesn't compose the coop layer."""
    for name in ("seamless-full", "single-full"):
        prof = load_profile(name, base=Path("profiles"))
        mg = next(m for m in prof["mods"] if m["id"] == "map-for-goblins")
        # me3-native, never "mods": under EML's load_delay the dll injects after
        # the world map is built and no icons appear.
        assert mg["install"] == "me3-native"
        # Nine MAIN files ship at once, one per overhaul -- an unpinned fetch
        # would grab the wrong build.
        assert mg["file_id"] == 48311
        assert not mg.get("requires_all_players", False)
    assert "map-for-goblins" not in [
        m["id"] for m in load_profile("gameplay-extras", base=Path("profiles"))["mods"]]


def test_experimental_keeps_its_rejected_notes():
    """The notes are what stop a candidate being re-added by someone who doesn't
    know it was already tried. Both of these failed on this machine."""
    prof = load_profile("experimental", base=Path("profiles"))
    text = Path("profiles/experimental.toml").read_text()
    assert prof["includes"] == ["seamless-full"]
    for rejected in ("questpath", "starlight-shards-rune-arcs"):
        assert rejected not in [m["id"] for m in prof["mods"]]
        assert rejected in text


def test_the_grace_talk_machine_is_merged_not_won():
    """Both mods ship this file and it is the whole mod on either side, so a
    last-one-wins mount silently disables one of them."""
    shared = load_profile("gameplay-extras", base=Path("profiles"))
    merge = next(x for x in shared["merges"]
                 if x["path"] == "script/talk/m00_00_00_00.talkesdbnd.dcx")
    assert merge["strategy"] == "esd-3way"
    assert set(merge["mods"]) == {"boss-resurrection-lite", "journey-with-melina"}
    assert merge["prefer"] == "boss-resurrection-lite"
    assert merge["vanilla"]["mod"] == "item-enemy-randomizer"


def _ships_regulation(path):
    """Whether a vendor archive carries a regulation.bin, or None when it takes
    an extractor this machine hasn't got.

    Sniffs zip by content and falls back to libarchive for the rest, the same
    way extract_archive picks its reader. Nexus serves .rar and .7z as readily
    as .zip, but sending zips through bsdtar as well would make this check
    unrunnable wherever bsdtar isn't installed -- most machines that run the
    suite. install._list_archive raises on a listing that fails, so an archive
    that could not be read never comes back as False.
    """
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        if shutil.which(install.EXTRACTOR) is None:
            return None
        names = install._list_archive(path)
    else:
        with z:
            names = z.namelist()
    return any(n.rsplit("/", 1)[-1] == "regulation.bin" for n in names)


def test_every_regulation_mod_is_named_in_the_regulation_merge():
    """An undeclared provider aborts the apply, so the merge has to name every
    mod that ships a regulation.bin -- in ANY profile composing this one, not
    just the ones installed here. Read out of the archives rather than listed by
    hand: a hardcoded set only ever records what was true when it was written,
    and this is exactly the check a newly added regulation mod has to trip."""
    shared = load_profile("gameplay-extras", base=Path("profiles"))
    merge = next(x for x in shared["merges"] if x["path"] == "regulation.bin")
    assert merge["prefer"] == "clevers-moveset"
    assert merge["strategy"] == "param-rows"

    lock = load_lock("mods.lock.toml")
    candidates = {m["id"]: m for prof in ("gameplay-extras", "experimental")
                  for m in load_profile(prof, base=Path("profiles"))["mods"]
                  if m.get("install") == "me3-package"}
    checked = 0
    unread = []
    for mod_id, mod in sorted(candidates.items()):
        asset = lock.get(mod_id, {}).get("asset")
        if not asset or not (Path("vendor") / asset).exists():
            continue                      # not fetched here; nothing to read
        ships = _ships_regulation(Path("vendor") / asset)
        if ships is None:
            unread.append(asset)      # needs an extractor we haven't got
            continue
        checked += 1
        if ships:
            assert mod_id in merge["mods"], (
                f"{mod_id} ships a regulation.bin but the merge doesn't name it")
            assert mod.get("requires_all_players") is True, mod_id
    if unread:
        # An archive dropped for want of an extractor is one this check did
        # not read, and a silent drop is indistinguishable from a clean pass.
        warnings.warn(f"not read, {install.EXTRACTOR} is not installed: "
                      + ", ".join(unread))
    if not checked:
        pytest.skip("no vendor archives could be read"
                    + (f" ({len(unread)} need {install.EXTRACTOR})" if unread else ""))


def _zip_with(tmp_path, *names):
    path = tmp_path / "mod.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, b"x")
    return path


def test_a_zip_is_read_without_the_external_extractor(tmp_path, monkeypatch):
    """Apply reads zips with the stdlib, so this has to as well -- routing them
    through libarchive made the check unrunnable wherever bsdtar isn't
    installed, which is most of the machines that run the suite."""
    monkeypatch.setattr(install, "_list_archive",
                        lambda *a, **k: pytest.fail("a zip must not need bsdtar"))
    assert _ships_regulation(_zip_with(tmp_path, "mod/regulation.bin")) is True
    assert _ships_regulation(_zip_with(tmp_path, "mod/regulation.bin.bak")) is False


def test_an_archive_that_cannot_be_listed_is_not_read_as_shipping_nothing(tmp_path, monkeypatch):
    """A failed listing and "no regulation.bin inside" are different answers.
    Conflating them lets a new regulation mod pass this check having been read
    not at all, which is exactly the case the check exists for."""
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/" + exe)
    monkeypatch.setattr(install, "_list_archive", _raise_bad_zip)
    broken = tmp_path / "mod.7z"
    broken.write_bytes(b"not an archive")
    with pytest.raises(zipfile.BadZipFile):
        _ships_regulation(broken)


def test_an_archive_needing_a_missing_extractor_reports_unread(tmp_path, monkeypatch):
    """Unread is its own answer: the caller drops these rather than counting
    them as checked, so a bsdtar-less box skips honestly instead of certifying
    archives it never opened."""
    monkeypatch.setattr(shutil, "which", lambda exe: None)
    unreadable = tmp_path / "mod.7z"
    unreadable.write_bytes(b"not an archive")
    assert _ships_regulation(unreadable) is None


def test_archives_that_could_not_be_read_do_not_count_as_checked(monkeypatch):
    """`checked` is the net under the whole check. If an unread archive counted
    toward it, a machine that can read none of them would go green having
    verified nothing rather than skipping."""
    monkeypatch.setitem(globals(), "_ships_regulation", lambda path: None)
    with pytest.raises(pytest.skip.Exception, match="no vendor archives could be read"):
        test_every_regulation_mod_is_named_in_the_regulation_merge()


def test_regulation_mods_in_the_shared_profile_are_required_of_everyone():
    """The merged regulation.bin is built from whichever profile is applied, so
    a regulation mod in the shared set gives partners a different file unless
    they run it too -- which reads as a failure to connect, not a desync."""
    shared = load_profile("gameplay-extras", base=Path("profiles"))
    merge = next(x for x in shared["merges"] if x["path"] == "regulation.bin")
    shared_ids = {m["id"] for m in shared["mods"]}
    for m in shared["mods"]:
        if m["id"] in merge["mods"]:
            assert m["requires_all_players"] is True, m["id"]
    # Every contributor is declared somewhere reachable, or the apply aborts on
    # a name nothing provides.
    trial_ids = {m["id"] for m in load_profile("experimental", base=Path("profiles"))["mods"]}
    for mod_id in merge["mods"]:
        assert mod_id in shared_ids | trial_ids, mod_id


def test_forever_buffs_keeps_its_packaging_workarounds():
    """Two things about this archive that a tidy-up would plausibly delete: the
    subdir, without which auto-placement can't find the root, and the prune of an
    editor artifact that would otherwise mount over vanilla systemparam."""
    shared = load_profile("gameplay-extras", base=Path("profiles"))
    fb = next(m for m in shared["mods"] if m["id"] == "forever-buffs")
    # Files/ holds two folders, so the single-wrapper descent gives up.
    assert fb["subdir"] == "Files/mod"
    # Two MAIN files ship; 39807 is the permanent-buff build, not this one.
    assert fb["file_id"] == 39767
    prune = next(p for p in shared["prunes"] if p["mod"] == "forever-buffs")
    assert "param/systemparam/systemparam.parambnd.dcx" in prune["paths"]


def test_no_profile_unpacks_the_windows_me3_build_into_tools():
    # me3's Windows components (me3.exe, me3-launcher.exe, me3_mod_host.dll) are
    # installed by the me3-host entry, which puts them where me3 actually looks
    # for them (~/.local/share/me3/windows-bin). A profile that also unpacked the
    # Windows .zip dropped a second, unread copy into tools/me3/ that nothing
    # launches and no uninstall removes.
    unpackers = {}
    for path in sorted(Path("profiles").glob("*.toml")):
        for mod in load_profile(path.stem, base=Path("profiles"))["mods"]:
            if mod.get("install") == "me3":
                unpackers.setdefault(path.stem, []).append(mod["id"])
    assert not unpackers, f"profiles still unpacking the Windows me3 build: {unpackers}"
