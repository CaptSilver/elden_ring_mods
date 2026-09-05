import tomllib
from pathlib import Path

from .errors import ErmError, PathError


def _load_toml(path):
    """Parse a TOML file, turning a bad one into an error main() can print.

    Both failure modes are ValueErrors — tomllib's TOMLDecodeError and
    read_text()'s UnicodeDecodeError — so neither is caught by the OSError and
    ErmError handlers between here and the top, and both escape as a raw
    traceback. The message names the file because tomllib's own carries only a
    line and column, and a stack has eleven candidate TOML files in it.

    OSError deliberately passes through: the CLI turns a missing profile into
    "unknown profile '<name>'" and that wording is worth keeping.
    """
    path = Path(path)
    try:
        return tomllib.loads(path.read_text())
    except ValueError as exc:
        raise ErmError(f"{path} is not valid TOML ({exc}) — fix the file and re-run") from exc


def _entry_key(entry):
    """Hashable, order-independent form of a [[merges]]/[[prunes]] TOML table.

    tomllib hands back plain dicts, which aren't hashable, and a table can
    hold a list (mods/paths) that isn't either -- so a raw `entry` can't be a
    dict key. Sorting by field name makes the result independent of the
    table's key order in the source TOML.
    """
    def freeze(v):
        # Recursive because a table can nest one: three-way merges declare
        # `vanilla = { mod = ..., member = ... }`, and a nested dict is no more
        # hashable than the outer one.
        if isinstance(v, dict):
            return tuple(sorted((k, freeze(x)) for k, x in v.items()))
        if isinstance(v, list):
            return tuple(freeze(x) for x in v)
        return v

    return freeze(entry)


def load_profile(name, base=Path("profiles"), _seen=None):
    """Load a profile, resolving an optional `includes = ["other", ...]` list.

    Included profiles are merged in first (in include order), then this
    profile's own `[[mods]]`. On a duplicate mod id the entry defined LATEST
    wins — a later include, or this profile's own entry, overrides an earlier
    one — so a profile can both compose others and override individual mods.
    A cycle in the include graph, or an unknown included profile, raises
    PathError. Callers already wrap a missing top-level profile.

    Also resolves an optional `excludes = ["other-profile", ...]` list — names
    of profiles this one is mutually exclusive with (e.g. two stacks that both
    edit regulation.bin). The resolved `excludes` is the union of every
    included profile's `excludes` plus this profile's own, de-duplicated,
    order-stable — so a profile that includes another inherits its excludes
    too, without having to repeat them.

    Also resolves optional `[[merges]]`, `[[prunes]]` and `[[renames]]` tables.
    A merge names a game-relative path two mods both ship, the strategy that
    resolves it, and which mod wins a genuine collision. A prune names files a
    mod ships that carry no content of its own. A rename moves a mod's file to
    the path the rest of the stack uses, so a packaging quirk doesn't read as a
    separate file. All three are unioned across the include chain and
    de-duplicated, so a merge declared once in gameplay-extras is inherited by
    every profile composing it.
    """
    base = Path(base)
    _seen = _seen or ()
    if name in _seen:
        raise PathError("profile include cycle: " + " -> ".join(_seen + (name,)))
    data = _load_toml(base / f"{name}.toml")
    merged, index = [], {}
    excludes, excludes_seen = [], set()
    merges, prunes, renames = [], [], []
    merge_seen, prune_seen, rename_seen = set(), set(), set()

    def add(mod):
        mid = mod["id"]
        if mid in index:
            merged[index[mid]] = mod          # latest definition wins, in place
        else:
            index[mid] = len(merged)
            merged.append(mod)

    def add_exclude(exc_name):
        if exc_name not in excludes_seen:
            excludes_seen.add(exc_name)
            excludes.append(exc_name)

    def add_merge(entry):
        # Two profiles in the include graph can declare the same merge; running
        # it twice would merge an already-merged file into itself. Key on the
        # WHOLE entry, not just (path, mods) -- two includes can name the same
        # path and mods but disagree on strategy/prefer, and keying on a subset
        # would silently keep whichever loaded first, discarding the other's
        # disagreement before conflicts._declare_merges ever gets a chance to
        # catch it.
        key = _entry_key(entry)
        if key not in merge_seen:
            merge_seen.add(key)
            merges.append(entry)

    def add_prune(entry):
        key = _entry_key(entry)
        if key not in prune_seen:
            prune_seen.add(key)
            prunes.append(entry)

    def add_rename(entry):
        # Deduped for a sharper reason than merges: a rename isn't idempotent.
        # Run it a second time and the source is already gone, so the guard
        # against clobbering the destination would fire on the mod's own file.
        key = _entry_key(entry)
        if key not in rename_seen:
            rename_seen.add(key)
            renames.append(entry)

    for inc in data.get("includes", []):
        try:
            included = load_profile(inc, base, _seen + (name,))
        except OSError as exc:
            raise PathError(f"profile '{name}' includes unknown profile '{inc}': {exc}") from exc
        for m in included["mods"]:
            add(m)
        for exc_name in included.get("excludes", []):
            add_exclude(exc_name)
        for entry in included.get("merges", []):
            add_merge(entry)
        for entry in included.get("prunes", []):
            add_prune(entry)
        for entry in included.get("renames", []):
            add_rename(entry)
    for m in data.get("mods", []):
        add(m)
    for exc_name in data.get("excludes", []):
        add_exclude(exc_name)
    for entry in data.get("merges", []):
        add_merge(entry)
    for entry in data.get("prunes", []):
        add_prune(entry)
    for entry in data.get("renames", []):
        add_rename(entry)
    data["mods"] = merged
    data["excludes"] = excludes
    data["merges"] = merges
    data["prunes"] = prunes
    data["renames"] = renames
    return data


def load_lock(path):
    p = Path(path)
    if not p.exists():
        return {}
    return _load_toml(p)


def set_mod(lock, mod_id, version, asset, sha256, source, file_id=None):
    """Pin one mod. `file_id` is Nexus's id for the exact file that was locked.

    Recording it is what makes a repin durable: the profile still names the
    superseded id, so without this the next plain fetch would select the old
    file and check it against the new locked hash.
    """
    lock[mod_id] = {
        "version": version, "asset": asset, "sha256": sha256, "source": source,
    }
    if file_id is not None:
        lock[mod_id]["file_id"] = str(file_id)


def _toml_escape(s):
    # Backslash first — it's the escape char, so escaping it after the others
    # would double-escape their inserted backslashes. tomllib rejects raw
    # control chars inside a basic string, so newline/CR/tab must go too or the
    # lockfile won't round-trip.
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def write_lock(path, data):
    lines = ["# Generated by erm. Pinned mod versions + hashes.", ""]
    for mod_id in sorted(data):
        lines.append(f"[{mod_id}]")
        for key in ("version", "file_id", "asset", "sha256", "source"):
            if key in data[mod_id]:
                lines.append(f'{key} = "{_toml_escape(data[mod_id][key])}"')
        lines.append("")
    Path(path).write_text("\n".join(lines))
