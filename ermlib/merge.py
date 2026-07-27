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


def _content_equal(a, b):
    """Whether two rows hold the same content, tolerating a differing stride.

    Ten shipped params derive a different row width per file: FromSoft's writer
    emits trailing padding SoulsFormats does not, and the derived stride absorbs
    it. Where the widths agree this is a plain byte comparison; where they don't,
    only the overlap is content and the excess is container padding. That
    tolerance is confined to the mismatched case on purpose — applying it
    everywhere would hide a real edit that happens to sit in the tail.
    """
    return a == b if len(a) == len(b) else a[:min(len(a), len(b))] == b[:min(len(a), len(b))]


def _merge_row(base, other, vanilla, entry_id, rid):
    """Three-way merge one row at byte granularity.

    Two mods editing the same row is not automatically a conflict: a param field
    is a contiguous byte range, so edits that touch disjoint bytes touched
    disjoint fields. Clever's Moveset and a weapon-buff mod collide on 39
    EQUIP_PARAM_WEAPON rows and never on a byte — the buff mod only ever sets one
    flag byte, while Clever's rewrites moveset and attack fields elsewhere in the
    row. Refusing those would mean giving up one mod entirely over a conflict
    that isn't one.

    Only a byte both sides changed, to different values, is a real conflict.

    The blind spot is a multi-byte field that two mods change in ways that happen
    not to share a byte — that would splice two values into one neither author
    wrote. Closing it needs field boundaries, i.e. a paramdef, which the transplant
    otherwise does not require. Merging at 4-byte granularity instead would be
    safe against that but wrong here: the one same-word case in the real data is
    byte 260 and byte 262 of a weapon row, which are two separate single-byte
    fields (an enum and a bit-flag), not one integer.
    """
    if not (len(base) == len(other) == len(vanilla)):
        raise MergeError(
            f"entry {entry_id} row {rid}: both sides changed it and the rows are "
            f"different widths ({len(base)}/{len(other)}/{len(vanilla)}), so the "
            f"edits can't be located")
    out = bytearray(base)
    for i, (b, o, v) in enumerate(zip(base, other, vanilla)):
        if o == v or o == b:
            continue                       # the other side didn't move this byte
        if b != v:
            raise MergeError(
                f"entry {entry_id} row {rid}: both sides changed byte {i} and "
                f"disagree (vanilla {v:#04x}, base {b:#04x}, other {o:#04x})")
        out[i] = o
    return bytes(out)


def _merge_param(base_blob, other_blob, van_blob, entry_id):
    """Three-way row merge of one param. Returns new bytes, or None if unchanged."""
    from .formats import param
    b, o, v = (param.read(x) for x in (base_blob, other_blob, van_blob))
    br = {r.id: r.data for r in b.rows}
    orr = {r.id: r.data for r in o.rows}
    vr = {r.id: r.data for r in v.rows}

    overwrite, insert, delete = {}, {}, []
    for rid in sorted(orr.keys() | vr.keys() | br.keys()):
        bd, od, vd = br.get(rid), orr.get(rid), vr.get(rid)
        same_ov = (od is None and vd is None) or (
            od is not None and vd is not None and _content_equal(od, vd))
        if same_ov:
            continue                       # the other side didn't touch this row
        same_bv = (bd is None and vd is None) or (
            bd is not None and vd is not None and _content_equal(bd, vd))
        if not same_bv:
            if bd is not None and od is not None and _content_equal(bd, od):
                continue                   # both sides made the same edit
            merged = _merge_row(bd, od, vd, entry_id, rid)
            if merged == bd:
                continue                   # everything the other side did, base already had
            overwrite[rid] = merged
            continue
        # Only the other side moved, so its version wins — but a row can only be
        # transplanted between files that lay rows out identically.
        if od is not None and b.stride != o.stride:
            raise MergeError(
                f"entry {entry_id} row {rid} differs, but the two files disagree on "
                f"row stride ({b.stride} vs {o.stride}) — the row can't be "
                f"transplanted without a paramdef to reinterpret it")
        if od is None:
            delete.append(rid)
        elif rid in br:
            overwrite[rid] = od
        else:
            insert[rid] = od

    if not (overwrite or insert or delete):
        return None
    patched = param.patch_rows(b, overwrite=overwrite, insert=insert)
    if delete:
        patched = param.replace_rows(patched, [r for r in patched.rows if r.id not in set(delete)])
    return param.write(patched)


def param_rows(base, other, vanilla):
    """Transplant `other`'s param-row edits onto `base`'s regulation.bin.

    Which rows to move is derived from the data, never configured: a row the
    other side changed relative to vanilla is an edit, and everything else is
    left alone. That is what keeps a narrow patch narrow — copying a whole param
    across would revert every row the base authored in it, which is measurably
    what happens with Clever's Moveset and its one-field edit to SpEffectParam
    row 174.

    Only params that actually change are re-serialised. Byte-comparing params to
    decide that would over-report roughly threefold, because tools disagree on
    whether the strings offset is rounded up to a 16-byte boundary.
    """
    from .formats import regulation
    base_entries = {e.id: e.data for e in regulation.entries(base)}
    other_entries = {e.id: e.data for e in regulation.entries(other)}
    van_entries = {e.id: e.data for e in regulation.entries(vanilla)}

    if not (set(base_entries) == set(other_entries) == set(van_entries)):
        raise MergeError(
            f"the three regulations hold different BND4 entries (base "
            f"{len(base_entries)}, other {len(other_entries)}, vanilla "
            f"{len(van_entries)})")

    from .formats.param import ParamError
    replacements = {}
    for eid, base_blob in base_entries.items():
        other_blob, van_blob = other_entries[eid], van_entries[eid]
        # Cheap exits that hold whatever the layout is, so a param neither side
        # touched never has to be parseable. Three of vanilla's own entries are
        # only readable after a tool has re-saved them: FromSoft stores a strings
        # offset past the end of the file, which SoulsFormats normalises. Both
        # mods carry the normalised copy, byte-identical to each other.
        if base_blob == other_blob or other_blob == van_blob:
            continue
        try:
            merged = _merge_param(base_blob, other_blob, van_blob, eid)
        except ParamError:
            # A layout the reader refuses. If the other side is byte-identical to
            # vanilla it has no edit to lose and base wins by default; if base is
            # the identical one, the other side's whole entry is the only change
            # there is. Anything else is a real edit we cannot read, so refuse
            # rather than quietly picking a side.
            if other_blob == van_blob:
                continue
            if base_blob == van_blob:
                replacements[eid] = other_blob
                continue
            raise MergeError(
                f"entry {eid} can't be parsed as a PARAM and both sides differ "
                f"from vanilla — refusing to guess which edit to keep")
        if merged is not None:
            replacements[eid] = merged
    return regulation.repack(base, replacements)


STRATEGIES = {"fmg-union": fmg_union, "fmg-3way": fmg_three_way,
              "param-rows": param_rows}
# Strategies that need the vanilla file the mods branched from. conflicts.py
# resolves it from the merge declaration and refuses if it isn't declared.
NEEDS_VANILLA = frozenset({"fmg-3way", "param-rows"})
