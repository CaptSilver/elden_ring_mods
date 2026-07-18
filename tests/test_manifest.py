from pathlib import Path
from ermlib.manifest import load_profile, write_lock, load_lock, set_mod


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
    assert len(prof["mods"]) == 16   # eac-toggler + unlock-the-fps dropped; randomizer disabled

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
    assert len(ids) == 18
    # single-player: no Seamless Co-op
    assert "seamless-coop" not in ids
    # the mods that desync co-op but work solo are included
    assert "pause-the-game" in ids
    assert "unlock-the-fps" in ids
    # the loaders/randomizer still route to their special install kinds
    me3 = next(m for m in prof["mods"] if m["id"] == "me3")
    assert me3["install"] == "me3"
    rnd = next(m for m in prof["mods"] if m["id"] == "item-enemy-randomizer")
    assert rnd["install"] == "randomizer"
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
    br = next(m for m in prof["mods"] if m["id"] == "boss-resurrection-lite")
    assert br["source"] == "nexus"
    assert br["nexus_id"] == 2790
    assert br["file_id"] == 24925          # the Lite build (no regulation.bin)
    assert br["install"] == "me3-package"
    assert br["kind"] == "gameplay"
    assert br["requires_all_players"] is True

    cl = next(m for m in prof["mods"] if m["id"] == "clevers-moveset")
    assert cl["nexus_id"] == 1928
    assert cl["file_id"] == 34558
    assert cl["install"] == "me3-package"
    assert cl["kind"] == "overhaul"
    assert cl["requires_all_players"] is True

    # summon-anywhere was removed (loaded fine, but useless in co-op).
    assert "summon-anywhere" not in [m["id"] for m in prof["mods"]]

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
