"""Tiny JSON install-state manifest: which files each mod put into Game/.

`erm apply`/`erm update` write this on every install so `erm uninstall` can
remove exactly what erm put there, instead of walking Game/ and guessing.
Lives at installed.json in the repo cwd — machine state, not shared,
gitignored.
"""
import json
from pathlib import Path

from .conflicts import MERGED_ID
from .errors import ErmError

DEFAULT_PATH = Path("installed.json")


def load_state(path=DEFAULT_PATH):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ErmError(f"installed.json is corrupted ({exc}) — delete it and re-apply") from exc


def record_install(state, mod_id, version, archive, files):
    state[mod_id] = {"version": version, "archive": archive, "files": list(files)}


def write_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True))


def forget(state, mod_id):
    state.pop(mod_id, None)


def record_me3_package(state, mod_id, version, archive, package):
    """Record a me3 content package (asset override extracted to tools/, not Game/).
    `package` is the repo-cwd-relative path to tools/me3/mods/<id>."""
    state[mod_id] = {"version": version, "archive": archive,
                     "kind": "me3-package", "package": package}


def record_randomizer(state, mod_id, version, archive, tools):
    """Record a randomizer generator (extracted to tools/<id>, not Game/).
    `tools` is the repo-cwd-relative path to that dir. Its own kind so it's
    tracked for the mutual-exclusion guard and `erm uninstall`, without counting
    as a me3 asset package (has_me3_packages/me3_packages ignore it)."""
    state[mod_id] = {"version": version, "archive": archive,
                     "kind": "randomizer", "tools": tools}


def record_me3_native(state, mod_id, version, archive, native):
    """Record a me3 native DLL mod (chainloaded, not a VFS asset override).
    `native` is the path to the .dll under tools/me3/natives/<id>/. Its own kind
    so me3_packages/has_me3_packages ignore it — a native doesn't change which
    launcher you need the way a loose-asset package does."""
    state[mod_id] = {"version": version, "archive": archive,
                     "kind": "me3-native", "native": native}


def me3_natives(state):
    """Sorted (mod_id, dll_path) for every recorded native. Sorted so the
    regenerated me3 profile is byte-deterministic regardless of install order."""
    return sorted((mid, e["native"]) for mid, e in state.items()
                  if e.get("kind") == "me3-native" and e.get("native"))


def me3_packages(state):
    """Sorted (mod_id, package_path) for every recorded me3 package. Sorted so the
    regenerated me3 profile is byte-deterministic regardless of install order."""
    return sorted((mid, e["package"]) for mid, e in state.items()
                  if e.get("kind") == "me3-package" and e.get("package"))


def has_me3_packages(state):
    return any(e.get("kind") == "me3-package" for e in state.values())


def record_merged(state, package):
    """Record the synthetic package holding merged conflict output.

    Recorded as a me3-package so me3profile.reconcile emits it like any other,
    but flagged `derived` and given no archive since it's computed from the
    other packages, not fetched. Nothing reads the `derived` flag back -- it's
    accurate metadata for a human skimming installed.json, not enforcement.
    What actually keeps update/fetch from trying to resolve MERGED_ID as a
    real mod is that it never appears in a profile or in mods.lock.toml, so
    those commands never even iterate over it.
    """
    state[MERGED_ID] = {"version": "derived", "archive": None,
                        "kind": "me3-package", "package": package,
                        "derived": True}
