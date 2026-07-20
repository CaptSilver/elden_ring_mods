"""Merge strategies for files two mods both claim.

me3 mounts one file per path, last writer wins, so two mods shipping the same
archive means one of them silently isn't there. Where the conflict is resolvable
we merge; where it isn't, conflicts.py refuses to guess.
"""
from .errors import ErmError
from .formats import bnd4, dcx, fmg


class MergeError(ErmError):
    """Two files could not be merged without losing content."""


def fmg_union(base, other):
    """Union the FMG text tables of two .msgbnd.dcx archives.

    `base` wins any id both sides define. That matters for exactly one entry in
    practice -- Clever's Moveset replaces a vanilla save-failure string with its
    own diagnostic note, and we keep Clever's.

    Returns a zlib-compressed DCX, not Kraken: there is no open-source Kraken
    encoder and the game reads zlib fine.
    """
    base_raw, other_raw = dcx.read(base), dcx.read(other)
    base_entries = bnd4.read(base_raw)
    other_tables = {e.id: fmg.read(e.data) for e in bnd4.read(other_raw)}

    extra = set(other_tables) - {e.id for e in base_entries}
    if extra:
        raise MergeError(
            f"the second archive has BND4 entries {sorted(extra)} that the first "
            f"doesn't — merging would drop them, so refusing rather than "
            f"reporting a partial merge as success")

    replacements = {}
    for entry in base_entries:
        incoming = other_tables.get(entry.id)
        if not incoming:
            continue
        table = fmg.read(entry.data)
        merged = dict(incoming)
        merged.update(table)               # base wins genuine collisions
        if merged != table:
            replacements[entry.id] = fmg.write(merged)
    return dcx.write_dflt(bnd4.rebuild(base_raw, replacements))


STRATEGIES = {"fmg-union": fmg_union}
