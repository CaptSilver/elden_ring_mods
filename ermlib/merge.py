"""Merge strategies for files two mods both claim.

me3 mounts one file per path, last writer wins, so two mods shipping the same
archive means one of them silently isn't there. Where the conflict is resolvable
we merge; where it isn't, conflicts.py refuses to guess.
"""
from .errors import ErmError
from .formats import bnd4, dcx, fmg


class MergeError(ErmError):
    """Two files could not be merged without losing content."""


class _Absent:
    """A row id that isn't in a table at all, as opposed to one present with no
    text. Two-way union conflated the two, which resurrected 1,062 rows Clever's
    had deliberately emptied."""

    def __repr__(self):
        return "<absent>"


ABSENT = _Absent()


def fmg_union(base, other):
    """Union the FMG text tables of two .msgbnd.dcx archives.

    When both sides define an id, base wins only if it has real text (not None).
    None means a structural absence, not an authored value, so we defer to the
    other side's text when base has none. This preserves Clever's Moveset's
    diagnostic note overriding a vanilla string, and handles game-patch wording
    drift where Clever's carries newer text than the paired mod.

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
        merged = {}
        for tid in table.keys() | incoming.keys():
            base_v = table.get(tid)
            # Base wins only if it has real text; None defers to other's content
            merged[tid] = base_v if base_v is not None else incoming.get(tid)
        if merged != table:
            replacements[entry.id] = fmg.write(merged)
    return dcx.write_dflt(bnd4.rebuild(base_raw, replacements))


def _fmg_tables(blob):
    raw = dcx.read(blob)
    return raw, {e.id: fmg.read(e.data) for e in bnd4.read(raw)}


def fmg_three_way(base, other, vanilla):
    """Merge two .msgbnd.dcx archives against the vanilla they both branched from.

    `fmg_union` can only ask "does base hold text here", which cannot distinguish
    an authored value from an untouched vanilla one. That makes it wrong in both
    directions once both sides have edits: preferring base discards the other
    side's work, preferring the other side discards base's. Comparing against
    vanilla answers the question the union was groping at — *who changed this* —
    and only a row both sides changed differently is a genuine conflict.

    ABSENT is a distinct sentinel from None because an id present with no text is
    an authored deletion, while an absent id is nothing at all. Conflating them
    is what lets a deleted row come back from the other side.
    """
    base_raw, base_tables = _fmg_tables(base)
    _, other_tables = _fmg_tables(other)
    _, van_tables = _fmg_tables(vanilla)

    if not (set(base_tables) == set(other_tables) == set(van_tables)):
        raise MergeError(
            f"the three archives hold different BND4 entries "
            f"(base {len(base_tables)}, other {len(other_tables)}, vanilla "
            f"{len(van_tables)}); merging would silently drop whichever side is "
            f"missing from the structural base")

    replacements = {}
    for eid, base_table in base_tables.items():
        other_table, van_table = other_tables[eid], van_tables[eid]
        merged = {}
        for tid in base_table.keys() | other_table.keys() | van_table.keys():
            b = base_table.get(tid, ABSENT)
            o = other_table.get(tid, ABSENT)
            v = van_table.get(tid, ABSENT)
            if o == b or o == v:
                keep = b               # nobody disagrees, or only base moved
            elif b == v:
                keep = o               # only the other side moved
            else:
                raise MergeError(
                    f"entry {eid} row {tid}: both sides changed it and differ — "
                    f"base {b!r}, other {o!r}, vanilla {v!r}")
            if keep is not ABSENT:
                merged[tid] = keep
        # Only re-serialise what actually changed: fmg.write is content-faithful
        # but not byte-faithful (it dedupes strings vanilla stores duplicated),
        # so rewriting an untouched entry would churn bytes for no reason.
        if merged != base_table:
            replacements[eid] = fmg.write(merged)
    return dcx.write_dflt(bnd4.rebuild(base_raw, replacements))


STRATEGIES = {"fmg-union": fmg_union, "fmg-3way": fmg_three_way}
# Strategies that need the vanilla file the mods branched from. conflicts.py
# resolves it from the merge declaration and refuses if it isn't declared.
NEEDS_VANILLA = frozenset({"fmg-3way"})
