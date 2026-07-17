"""Tiny JSON install-state manifest: which files each mod put into Game/.

`erm apply`/`erm update` write this on every install so `erm uninstall` can
remove exactly what erm put there, instead of walking Game/ and guessing.
Lives at installed.json in the repo cwd — machine state, not shared,
gitignored.
"""
import json
from pathlib import Path

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


def me3_packages(state):
    """Sorted (mod_id, package_path) for every recorded me3 package. Sorted so the
    regenerated me3 profile is byte-deterministic regardless of install order."""
    return sorted((mid, e["package"]) for mid, e in state.items()
                  if e.get("kind") == "me3-package")


def has_me3_packages(state):
    return any(e.get("kind") == "me3-package" for e in state.values())
