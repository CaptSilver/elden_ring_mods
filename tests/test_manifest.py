from pathlib import Path
from ermlib.manifest import load_profile, write_lock, load_lock, set_mod


def test_seamless_only_profile_has_ersc():
    prof = load_profile("seamless-only", base=Path("profiles"))
    ids = [m["id"] for m in prof["mods"]]
    assert "seamless-coop" in ids
    ersc = next(m for m in prof["mods"] if m["id"] == "seamless-coop")
    assert ersc["source"] == "github"
    assert str(ersc["repo_id"]) == "497113840"


def test_lock_roundtrip(tmp_path):
    lock = {}
    set_mod(lock, "seamless-coop", version="v1.9.8", asset="Seamless.zip",
            sha256="1a956a30", source="github")
    p = tmp_path / "mods.lock.toml"
    write_lock(p, lock)
    back = load_lock(p)
    assert back["seamless-coop"]["version"] == "v1.9.8"
    assert back["seamless-coop"]["sha256"] == "1a956a30"
