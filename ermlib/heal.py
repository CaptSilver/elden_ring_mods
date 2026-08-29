"""Rebuild the stack against the game build that is actually installed.

The merge engine is already a rebase engine: param-rows transplants a mod's
authored rows onto a base file. Swap the base for the patched game's own
regulation.bin and every regulation mod forward-ports by itself -- provided no
param's row layout moved, which is what the layout gate certifies.

Planning is separated from execution so a dry run costs nothing and the risky
part is testable without a filesystem. This is the code that can quietly
corrupt a regulation; it does not get to hide inside apply.
"""
import struct
from pathlib import Path

from .errors import ErmError
from .formats import param, regulation

# Lives under tools/, which is already gitignored runtime state -- these are
# derived artifacts, not something a fresh clone should carry.
BASELINE_DIR = Path("tools/baselines")


class HealError(ErmError):
    """A heal could not be planned or completed."""


def baseline_path(regver, base=BASELINE_DIR):
    return Path(base) / f"regulation-{regver}.bin"


def adopt_baseline(game_dir, live, base=BASELINE_DIR):
    """Take the installed game's regulation.bin as the vanilla merge baseline.

    Keyed by build version and never overwritten. Every historical baseline is
    kept so an old merge can be reproduced, and so a tampered install has
    something known-good to fall back to.
    """
    dest = baseline_path(live.regulation, base)
    if dest.exists():
        return dest
    src = Path(game_dir) / "regulation.bin"
    try:
        blob = src.read_bytes()
    except OSError as exc:
        raise HealError(f"can't read {src}: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return dest


# paramdef_data_version sits at 0x08 of a PARAM header; param.read keeps the
# original header verbatim, so read it back from there rather than re-parsing.
_PARAMDEF_VERSION_AT = 0x08


def param_layouts(blob):
    """{table name: (row stride, paramdef_data_version)}, None where unreadable.

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
        out[name] = (p.stride, pdv)
    return out


def layout_gate(baseline_blob, mod_blobs):
    """Which mod tables cannot be byte-transplanted onto this baseline.

    param-rows moves whole rows between files, so it is only sound while both
    sides agree on the row stride AND the paramdef those bytes are laid out by.
    A patch that changes either makes a transplant corrupt data silently, which
    is exactly the failure worth stopping the run for.

    Tables the baseline doesn't carry are skipped: that is a merge question,
    not a transplant-safety one.
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
            stride, pdv = layout
            base_stride, base_pdv = base_layout
            if stride != base_stride:
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
