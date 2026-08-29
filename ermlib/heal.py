"""Rebuild the stack against the game build that is actually installed.

The merge engine is already a rebase engine: param-rows transplants a mod's
authored rows onto a base file. Swap that base for the patched game's own
regulation.bin and every regulation mod forward-ports by itself -- provided no
param's row layout moved, which is what the layout gate certifies.

"Baseline" throughout this module means that base: merge.param_rows' `base`
argument, the file rows are transplanted ONTO. It is never that function's
`vanilla`, which stays the older regulation the mods were built against and is
what says which rows a mod authored in the first place. The two arguments have
opposite effects, and passing the new game's regulation as `vanilla` would
read every row the patch changed as a mod edit and revert it.

Planning is separated from execution so a dry run costs nothing and the risky
part is testable without a filesystem. This is the code that can quietly
corrupt a regulation; it does not get to hide inside apply.
"""
import hashlib
import struct
from pathlib import Path
from typing import NamedTuple

from .errors import ErmError
from .formats import param, regulation
from . import gamebuild
from .gamebuild import read_regulation_version

# Lives under tools/, which is already gitignored runtime state -- these are
# derived artifacts, not something a fresh clone should carry.
BASELINE_DIR = Path("tools/baselines")


class HealError(ErmError):
    """A heal could not be planned or completed."""


def baseline_path(regver, base=BASELINE_DIR):
    return Path(base) / f"regulation-{regver}.bin"


def adopt_baseline(game_dir, live, base=BASELINE_DIR):
    """Keep the installed game's regulation.bin as the merge baseline.

    The baseline is what rows get transplanted onto, not what they are compared
    against -- see the module docstring on why those must not be swapped.

    Keyed by build version and never overwritten. Every historical baseline is
    kept so an old merge can be reproduced, and so a tampered install has
    something known-good to fall back to.

    Before writing, the blob is re-hashed against `live.regulation_sha` -- the
    digest it was identified by when the plan was built. Anything else means
    the install changed underneath the heal between planning and adopting, and
    the bytes in hand are no longer the ones that were classified as safe.
    """
    dest = baseline_path(live.regulation, base)
    if dest.exists():
        return dest
    src = Path(game_dir) / "regulation.bin"
    try:
        blob = src.read_bytes()
    except OSError as exc:
        raise HealError(f"can't read {src}: {exc}") from exc
    got = hashlib.sha256(blob).hexdigest()
    if got != live.regulation_sha:
        raise HealError(
            f"{src} changed between planning and adopting (identified "
            f"{live.regulation_sha[:12]}, read {got[:12]}) — re-run so the heal "
            f"plans against the file as it now stands")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return dest


# paramdef_data_version sits at 0x08 of a PARAM header; param.read keeps the
# original header verbatim, so read it back from there rather than re-parsing.
_PARAMDEF_VERSION_AT = 0x08


class Layout(NamedTuple):
    """What a param's shape has to agree on before rows can be transplanted."""
    rows: int
    stride: int
    paramdef: int


def param_layouts(blob):
    """{table name: Layout}, None where the entry cannot be read.

    Three of vanilla's own entries carry a strings offset past the end of the
    file. FromSoft writes them that way and SoulsFormats normalises them on
    re-save, so a mod's copy parses where the game's own does not. Recording
    None beats raising: the gate asks whether a transplant is safe, and a table
    nobody transplants never has to be read. The merge already refuses to guess
    when an entry it cannot read differs on both sides.
    """
    out = {}
    for entry in regulation.entries(blob):
        if not entry.name.lower().endswith(".param"):
            continue
        name = entry.name.rsplit("\\", 1)[-1]
        try:
            p = param.read(entry.data)
        except param.ParamError:
            out[name] = None
            continue
        pdv, = struct.unpack_from("<H", p.header, _PARAMDEF_VERSION_AT)
        out[name] = Layout(len(p.rows), p.stride, pdv)
    return out


def layout_gate(baseline_blob, mod_blobs):
    """Which mod tables cannot be byte-transplanted onto this baseline.

    param-rows moves whole rows between files, so it is only sound while both
    sides agree on the row stride AND the paramdef those bytes are laid out by.
    A patch that changes either makes a transplant corrupt data silently, which
    is exactly the failure worth stopping the run for.

    Tables the baseline doesn't carry are skipped: that is a merge question,
    not a transplant-safety one. So is a table either side can't be read --
    param_layouts records those as None and there is nothing to compare.

    A one-row param's stride is NOT compared, only its paramdef version: with
    no inter-row gap the derived width folds in whatever the file keeps before
    the strings block -- the param type name, not alignment padding -- and the
    game and a mod's re-saved copy round that differently. See
    param.strides_comparable.
    """
    base = param_layouts(baseline_blob)
    problems = []
    for mod_id, blob in mod_blobs:
        for name, layout in param_layouts(blob).items():
            if name not in base:
                continue
            base_layout = base[name]
            if base_layout is None or layout is None:
                # Nothing to compare. param_rows already refuses to guess when
                # an unreadable entry differs on both sides, so alarming here
                # would only fire every run on params nobody transplants.
                continue
            stride, pdv = layout.stride, layout.paramdef
            base_stride, base_pdv = base_layout.stride, base_layout.paramdef
            if (param.strides_comparable(layout.rows, base_layout.rows)
                    and stride != base_stride):
                problems.append(
                    f"{mod_id}: {name} row stride {stride} != baseline {base_stride} "
                    "— rows can't be transplanted, this mod needs an update for "
                    "the new game build")
            elif pdv != base_pdv:
                problems.append(
                    f"{mod_id}: {name} paramdef version {pdv} != baseline {base_pdv} "
                    "— field layout moved, this mod needs an update for the new "
                    "game build")
    return tuple(problems)


def rows_by_table(blob):
    """{table name: {row id: row bytes}} for every readable param in a regulation.

    Three of vanilla's own entries carry a strings offset past the end of the
    file -- FromSoft writes them that way and SoulsFormats normalises them on
    re-save, so a mod's copy parses where the game's own does not. Those tables
    are left out of the result rather than raising: merge.param_rows never
    row-splices an unreadable entry, it takes the whole entry from one side or
    raises MergeError, so there are no transplanted rows in them to verify.
    """
    out = {}
    for entry in regulation.entries(blob):
        if not entry.name.lower().endswith(".param"):
            continue
        try:
            p = param.read(entry.data)
        except param.ParamError:
            continue
        out[entry.name.rsplit("\\", 1)[-1]] = {r.id: r.data for r in p.rows}
    return out


def verify_rebase(merged_blob, baseline_blob, mod_blobs, live):
    """Prove a rebase kept what it was supposed to keep.

    Two properties, both machine-checkable, and both silent failures otherwise:

    - the output claims the installed build, so a stale merge can't be mounted
      over a patched game again;
    - every row exactly one mod authored survives byte-identical, and every row
      the new baseline contributed is still there.

    Rows authored by SEVERAL mods are skipped: those are resolved by `prefer`
    and reported by the merge itself, so demanding all of them survive would
    fail every legitimate preferred merge. "The new baseline contributed it"
    means the row is new to every mod, not merely unauthored -- see the
    `claimed` comment below for why that distinction matters. Tables
    rows_by_table could not read on either side are likewise absent from both
    `base` and `merged`/`authored`, so they are silently skipped here too --
    consistent with the same tables never being row-spliced by the merge in
    the first place.
    """
    problems = []
    got = read_regulation_version(merged_blob)
    if got != live.regulation:
        problems.append(
            f"merged regulation.bin claims {got}, game is {live.regulation}")

    base = rows_by_table(baseline_blob)
    merged = rows_by_table(merged_blob)

    # `claimed` is every row any mod's own file even mentions, authored or not.
    # A mod ships a table in full, so an untouched row shows up identical to
    # base -- that is not a baseline contribution, it is a row the mod already
    # had an opinion on (silence), and its fate after the merge is between the
    # mod and the merge, not something this check promises. Only a row no
    # mod's file mentions at all -- because it didn't exist when that mod was
    # built -- is purely "the new baseline contributed it".
    authored = {}
    claimed = set()
    for mod_id, blob in mod_blobs:
        for table, rows in rows_by_table(blob).items():
            for rid, data in rows.items():
                claimed.add((table, rid))
                if base.get(table, {}).get(rid) == data:
                    continue
                authored.setdefault((table, rid), {})[mod_id] = data

    for (table, rid), by_mod in sorted(authored.items()):
        if len(by_mod) != 1:
            continue
        mod_id, data = next(iter(by_mod.items()))
        if merged.get(table, {}).get(rid) != data:
            problems.append(
                f"{mod_id}: {table} row {rid} did not survive the rebase")

    for table, rows in base.items():
        for rid, data in rows.items():
            if (table, rid) in claimed:
                continue
            if rid not in merged.get(table, {}):
                problems.append(
                    f"baseline {table} row {rid} was dropped by the rebase")

    return tuple(problems)


class Action(NamedTuple):
    """One step of a heal. `data` carries whatever the executor needs."""
    kind: str
    detail: str
    data: object = None


def plan_heal(stamped, live, reharden=True, launcher_stale=False):
    """The ordered steps that bring this stack back onto the installed build.

    Pure: no filesystem, no network. A dry run is this function and nothing
    else, which is why the risky ordering decisions are testable at all.
    """
    kind = gamebuild.classify(stamped, live)
    if kind == gamebuild.UNCHANGED:
        return ()
    if kind == gamebuild.TAMPERED:
        return (Action(
            "refuse",
            f"Game/regulation.bin changed but the game was not patched "
            f"(exe {live.exe}, buildid {live.steam_buildid} unchanged) — "
            f"something overwrote the install's regulation. Refusing to adopt "
            f"it as vanilla. Run Steam → Verify integrity of game files, or "
            f"heal against the last known-good baseline.",
            stamped),)

    actions = []
    if kind == gamebuild.PATCHED:
        actions.append(Action("adopt-baseline",
                              f"take the {live.regulation} regulation as the merge baseline",
                              live.regulation))
    # Repin before the gate: an updated mod may be exactly what makes a mod's
    # rows transplantable onto the new baseline.
    actions.append(Action("repin", "re-resolve mod pins against upstream", None))
    if kind == gamebuild.PATCHED:
        actions.append(Action("gate", "check param row layouts against the baseline", None))
        actions.append(Action("rebuild", "rebuild every merge against the new baseline", None))
        actions.append(Action("verify", "check the rebase kept every authored row", None))
    if reharden and launcher_stale:
        actions.append(Action("reharden",
                              "re-copy eldenring.exe over the stale hardened launcher (sudo)",
                              None))
    actions.append(Action("stamp", f"record build {live.app}", live))
    return tuple(actions)
