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
