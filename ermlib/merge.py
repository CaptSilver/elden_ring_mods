"""Merge strategies for files two mods both claim.

me3 mounts one file per path, last writer wins, so two mods shipping the same
archive means one of them silently isn't there. Where the conflict is resolvable
we merge; where it isn't, conflicts.py refuses to guess.
"""
from typing import NamedTuple

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


class RowCollision(NamedTuple):
    """The game and a mod each added a different row under one id, with no
    ancestor row to locate either edit against."""
    entry_id: int
    row_id: int


class StaleContributor(NamedTuple):
    """A merge names a mod that is installed but ships nothing at the path, so
    the declaration has gone stale -- the mod dropped the file in an update."""
    path: str
    mods: tuple


class RivalRow(NamedTuple):
    """Two mods each added a different row under one id, with no ancestor row
    to locate either edit against. The merge spec's `prefer` leads the fold, so
    the leading mod's row is the one kept."""
    entry_id: int
    row_id: int


class UnreadableParam(NamedTuple):
    """A param entry the reader refuses, where only the other side had an edit,
    so its whole table was taken rather than merged row by row."""
    entry_id: int


def _require_same_entries(base_keys, other_keys, van_keys, noun):
    """Refuse three archives that don't declare the same BND4 entries.

    Every three-way strategy here rebuilds its output from the base's entry
    list, so an entry one side is missing has nowhere to go: "only the other
    side moved" can't be told from "base authored this and vanilla just doesn't
    happen to carry it", and guessing wrong silently discards the preferred
    mod's own content. That is one rule, so it lives in one place -- it was
    hand-copied into three before, and the copy in esd_three_way drifted into
    a one-sided subset check that shipped exactly that data loss.

    Names the ids that differ and who holds them, capped so a 194-entry
    regulation doesn't flood the terminal.
    """
    sides = (("base", set(base_keys)), ("other", set(other_keys)),
             ("vanilla", set(van_keys)))
    odd = sorted(set().union(*(s for _, s in sides))
                 - set.intersection(*(s for _, s in sides)))
    if not odd:
        return
    shown = "; ".join(
        f"entry {eid}: in {', '.join(n for n, s in sides if eid in s)}"
        for eid in odd[:5])
    more = f" (and {len(odd) - 5} more)" if len(odd) > 5 else ""
    raise MergeError(
        f"the three {noun} hold different BND4 entries — {shown}{more}; "
        f"merging would silently drop whichever side is missing from the "
        f"structural base")


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

    _require_same_entries(base_tables, other_tables, van_tables, "archives")

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

    Ten shipped params derive a different row width per file: the bytes between
    the last row and the strings block — in practice the param type name, not
    padding — fold into a one-row param's derived stride, so the same row can
    measure a few bytes different from file to file. Where the widths agree
    this is a plain byte comparison; where they don't, only the overlap is the
    row's actual content, and the excess belongs to whichever file it came
    from rather than being an edit. That tolerance is confined to the
    mismatched case on purpose — applying it everywhere would hide a real edit
    that happens to sit in the tail.
    """
    return a == b if len(a) == len(b) else a[:min(len(a), len(b))] == b[:min(len(a), len(b))]


def _fit_to_stride(row, base_row, stride, entry_id, rid):
    """Re-widen a transplanted row to the base file's row width.

    Reachable whenever the two strides aren't compared, which is whenever
    either side has one row or fewer -- so a multi-row param on the other side
    can land here too, not just a one-row param. The excess beyond the row
    itself is whatever the base file keeps between the last row and the
    strings block -- in practice the param type name -- and it must come from
    the base, never be synthesised or zero-filled: it's the base file's own
    content, so the base keeps its own tail. A non-zero excess on the
    transplanted side refuses because it's real content rather than padding,
    and losing it silently is exactly what the stride guard exists to prevent.
    """
    if len(row) == stride:
        return row
    if len(row) > stride:
        if row[stride:].strip(b"\x00"):
            raise MergeError(
                f"entry {entry_id} row {rid} is {len(row)} bytes where the base "
                f"lays rows out {stride} wide, and the extra bytes are not "
                f"padding — the edit can't be fitted without a paramdef")
        return row[:stride]
    tail = (base_row or b"")[len(row):stride]
    return row + tail + b"\x00" * (stride - len(row) - len(tail))


def _duplicate_ids(param):
    """The ids a param carries more than once.

    Legal and shipped: RandomAppearParam holds 5,322 rows for 5,296 unique ids,
    and 20 of the shadowed ids carry different bytes in their two copies, so
    they are distinct records that happen to share a number rather than
    redundant copies.
    """
    seen, dupes = set(), set()
    for row in param.rows:
        (dupes if row.id in seen else seen).add(row.id)
    return dupes


def _rows_identical(x, y):
    """Whether two params hold the same rows in the same order.

    Positional rather than keyed, because this is used exactly where an id
    doesn't identify a row. Content comparison goes through _content_equal for
    the same reason everything else here does: strides differ per file, so a
    raw byte compare would report an edit that isn't one.
    """
    return len(x.rows) == len(y.rows) and all(
        a.id == b.id and _content_equal(a.data, b.data)
        for a, b in zip(x.rows, y.rows))


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
    if base is None or other is None or vanilla is None:
        # One side has no such row at all -- the row is new to both sides, or
        # one deleted what the other edited. Byte-granularity merging needs
        # three versions to say who moved which byte, and picking a survivor
        # without that is the silent data loss the whole strategy avoids.
        absent = " and ".join(
            n for n, v in (("base", base), ("other", other), ("vanilla", vanilla))
            if v is None)
        raise MergeError(
            f"entry {entry_id} row {rid}: both sides changed it, but the row is "
            f"absent from {absent} — there is no common version to locate the "
            f"edits against")
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


def _merge_param(base_blob, other_blob, van_blob, entry_id, notes=None,
                 base_is_game=False):
    """Three-way row merge of one param. Returns new bytes, or None if unchanged."""
    from .formats import param
    b, o, v = (param.read(x) for x in (base_blob, other_blob, van_blob))
    dupes = _duplicate_ids(b) | _duplicate_ids(o) | _duplicate_ids(v)
    if dupes:
        # Everything below keys rows by id, which here silently collapses two
        # distinct records to the last one and makes a shadowed edit compare
        # equal to vanilla -- dropped with the merge still reporting success.
        # Decide the whole table positionally instead: if either side is
        # unchanged the answer needs no row lookup, and if both moved there is
        # no way to say which copy an edit belongs to. param.patch_rows refuses
        # duplicates too, but the diff above would have guessed long before it
        # ever gets called.
        if _rows_identical(o, v):
            return None                    # the other side has no edit to carry
        if _rows_identical(b, v):
            return other_blob              # base has nothing to lose
        raise MergeError(
            f"entry {entry_id} carries row ids {sorted(dupes)[:5]} more than "
            f"once and both sides changed the param, so an edit can't be "
            f"located by id — refusing to guess which copy moved")

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
            if bd is None and od is None:
                # Both sides dropped it. That is agreement, not a conflict --
                # and `continue` rather than a delete, because the row already
                # isn't in b.rows for replace_rows to remove. (vd is
                # necessarily present: od is None with vd None is same_ov.)
                continue
            if bd is not None and od is not None and _content_equal(bd, od):
                continue                   # both sides made the same edit
            if vd is None and bd is not None and od is not None:
                # Neither side inherited this row, so there is no ancestor to
                # locate either edit against. Renumbering the loser is not an
                # option either: it keeps the bytes and loses the behaviour --
                # a shop row is reached by id range, so a fresh id puts it
                # outside the block that reaches it. Someone has to win.
                if base_is_game:
                    # The game added one in a patch and a mod added a different
                    # one under the same id. The mod's wins: the point of the
                    # stack is the mod's content.
                    if notes is not None:
                        notes.append(RowCollision(entry_id, rid))
                    overwrite[rid] = od
                    continue
                # Two mods each invented a row under one id. Fold order decides
                # it, and fold order is not incidental -- `prefer` in the merge
                # spec sets which mod leads, so the profile has already said
                # which one wins a tie. Keeping the leader's row (no overwrite)
                # is that answer. Named, never silent: the dropped row is a
                # mod's authored content and the report has to say it went.
                if notes is not None:
                    notes.append(RivalRow(entry_id, rid))
                continue
            merged = _merge_row(bd, od, vd, entry_id, rid)
            if merged == bd:
                continue                   # everything the other side did, base already had
            overwrite[rid] = merged
            continue
        # Only the other side moved, so its version wins — but a row can only be
        # transplanted between files that lay rows out identically.
        if od is None:
            delete.append(rid)
            continue
        if b.stride != o.stride:
            if param.strides_comparable(len(b.rows), len(o.rows)):
                raise MergeError(
                    f"entry {entry_id} row {rid} differs, but the two files disagree on "
                    f"row stride ({b.stride} vs {o.stride}) — the row can't be "
                    f"transplanted without a paramdef to reinterpret it")
            # A one-row param's derived width folds in whatever the base file
            # keeps before the strings block -- the param type name, not
            # alignment padding -- so a mismatch here says nothing about where
            # the fields sit. Refusing would block every rebase onto the game's
            # own regulation, where ten shipped params measure wider than the
            # same table in a mod's re-saved copy.
            od = _fit_to_stride(od, bd, b.stride, entry_id, rid)
        if rid in br:
            overwrite[rid] = od
        else:
            insert[rid] = od

    if not (overwrite or insert or delete):
        return None
    patched = param.patch_rows(b, overwrite=overwrite, insert=insert)
    if delete:
        patched = param.replace_rows(patched, [r for r in patched.rows if r.id not in set(delete)])
    return param.write(patched)


def param_rows(base, other, vanilla, notes=None, base_is_game=False):
    """Transplant `other`'s param-row edits onto `base`'s regulation.bin.

    `base_is_game` says the base is the installed game's own regulation, which
    the caller supplied to lead the fold -- not another mod's file. It is the
    only thing that can tell "the patch added this row" from "the mod folded
    before this one added it", and the two want opposite answers.

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
    from .formats import bnd4, regulation
    # Decrypt the base once and keep the payload -- rebuilding from `base` again
    # at the end would AES the same 2 MB a second time for nothing.
    base_payload = regulation.unpack(base)
    base_entries = {e.id: e.data for e in bnd4.read(base_payload)}
    other_entries = {e.id: e.data for e in regulation.entries(other)}
    van_entries = {e.id: e.data for e in regulation.entries(vanilla)}

    _require_same_entries(base_entries, other_entries, van_entries, "regulations")

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
            merged = _merge_param(base_blob, other_blob, van_blob, eid, notes=notes,
                                  base_is_game=base_is_game)
        except ParamError:
            # A layout the reader refuses. If base is byte-identical to vanilla
            # then the other side's whole entry is the only change there is, and
            # swapping it in loses nothing -- but it's still a whole table
            # substituted sight-unseen, so say so. (The mirror case, other side
            # identical to vanilla, never reaches here: the cheap exit above
            # already continued on it.) Anything else is a real edit we cannot
            # read, so refuse rather than quietly picking a side.
            if base_blob == van_blob:
                replacements[eid] = other_blob
                if notes is not None:
                    notes.append(UnreadableParam(eid))
                continue
            raise MergeError(
                f"entry {eid} can't be parsed as a PARAM and both sides differ "
                f"from vanilla — refusing to guess which edit to keep")
        if merged is not None:
            replacements[eid] = merged
    # Never short-circuit an empty `replacements` back to `base`: the game's own
    # regulation declares a 64 MiB DCX window that me3 cannot load, and going
    # back through regulation.pack is what normalises it.
    return regulation.pack(bnd4.rebuild(base_payload, replacements),
                           base[:regulation.IV_SIZE])


def tpf_union(base, other):
    """Add `other`'s new textures to `base`'s menu atlas set.

    Two-way rather than three-way, because there is no vanilla side to be had:
    01_common.tpf.dcx exists only inside the game's .bdt archives, which erm
    can't read. That's tolerable here in a way it wasn't for FMG text — a
    texture is keyed by name, and a mod adding icons ships them under a name of
    its own (SB_Status_GRO_00), so "new name" identifies the contribution
    without needing to know what vanilla held.

    Where both sides ship the same name, base wins. Both mods carry the whole
    ~204 MB vanilla set, so the shared names are overwhelmingly identical
    copies; where they differ, the preferred mod's is the one with its own icons
    layered in and the other's is untouched vanilla.
    """
    from .formats import tpf
    base_archive = tpf.read(dcx.read(base))
    other_archive = tpf.read(dcx.read(other))
    have = {t.name for t in base_archive.textures}
    extra = tuple(t for t in other_archive.textures if t.name not in have)
    if not extra:
        return base
    return dcx.write_dflt(
        tpf.write(base_archive.with_textures(base_archive.textures + extra)))


def esd_three_way(base, other, vanilla, notes=None):
    """Merge two mods' edits to a .talkesdbnd.dcx.

    The container is handled here and the state machines next door in esdmerge.
    An entry only one side changed is swapped whole, which costs nothing and
    covers the common case of two mods touching different machines in the same
    bundle -- it's how a second, unrelated edit crosses with no graph work at
    all. Only an entry both sides changed reaches esdmerge, and even then only
    a group both sides touched goes through the full three-way replay; most of
    a shared .esd is still a wholesale copy at the group level, inside esdmerge
    itself.

    `notes` is what esdmerge hands back when it couldn't carry something across
    cleanly -- passed in by the caller (conflicts.resolve, ultimately the apply
    report) rather than printed here, since a library function has no business
    deciding how its own diagnostics reach a user.

    All three archives must declare the same BND4 entries, matching
    fmg_three_way and param_rows: without vanilla to compare an entry against,
    "only the other side moved" can't be told apart from "base authored this
    and vanilla just doesn't happen to carry it", and guessing wrong means
    silently discarding the preferred mod's own content in favour of the
    other's.
    """
    from . import esdmerge
    from .formats import esd

    base_raw = dcx.read(base)
    base_entries = {e.id: e.data for e in bnd4.read(base_raw)}
    other_entries = {e.id: e.data for e in bnd4.read(dcx.read(other))}
    van_entries = {e.id: e.data for e in bnd4.read(dcx.read(vanilla))}

    _require_same_entries(base_entries, other_entries, van_entries, "archives")

    replacements = {}
    for eid, base_blob in base_entries.items():
        other_blob = other_entries[eid]
        van_blob = van_entries[eid]
        if other_blob == base_blob or other_blob == van_blob:
            continue                              # base's own edit, or nobody touched it
        if base_blob == van_blob:
            replacements[eid] = other_blob        # only the other side moved
            continue
        merged, entry_notes = esdmerge.merge(
            esd.read(base_blob), esd.read(other_blob), esd.read(van_blob))
        blob = esd.write(merged)
        # Check the bytes, not the graph esdmerge already checked on the way
        # out: re-reading is what puts the writer under the same invariants as
        # everything else, and esd.read adds two of its own on the way through
        # -- that the five tables tile the file, and that the state rows the
        # header declares are the ones the groups actually span. Nothing else
        # in the pipeline ever reads back what it wrote, so without this the
        # first reader of a merged .esd is the game.
        esdmerge.check(esd.read(blob))
        replacements[eid] = blob
        if notes is not None:
            notes.extend(entry_notes)
    return dcx.write_dflt(bnd4.rebuild(base_raw, replacements))


def describe_note(note):
    """Render a strategy's structured note as the sentence an apply report shows.

    This indirection means a caller displaying a note doesn't need to import
    esdmerge itself, or know which strategy produced it, to do it.
    """
    if isinstance(note, RowCollision):
        return (f"entry {note.entry_id} row {note.row_id}: the game and a mod "
                f"each added a different row under this id — kept the mod's, "
                f"dropped the game's")
    if isinstance(note, StaleContributor):
        return (f"{', '.join(note.mods)} no longer ship this path, so the merge "
                f"naming them isn't happening — whatever still provides it now "
                f"mounts whole. Drop them from the [[merges]] entry, and prune "
                f"the survivor if it's a stale copy of a game file")
    if isinstance(note, RivalRow):
        return (f"entry {note.entry_id} row {note.row_id}: two mods each added "
                f"a different row under this id — kept the preferred mod's, "
                f"dropped the other's")
    if isinstance(note, UnreadableParam):
        return (f"entry {note.entry_id} can't be read as a PARAM — the base "
                f"still matched vanilla there, so the mod's whole table was "
                f"taken instead of merging it row by row")
    from . import esdmerge
    return esdmerge.describe(note)


STRATEGIES = {"fmg-union": fmg_union, "fmg-3way": fmg_three_way,
              "param-rows": param_rows, "tpf-union": tpf_union,
              "esd-3way": esd_three_way}
# Strategies that need the vanilla file the mods branched from. conflicts.py
# resolves it from the merge declaration and refuses if it isn't declared.
NEEDS_VANILLA = frozenset({"fmg-3way", "param-rows", "esd-3way"})
# Strategies that report back things they couldn't carry over cleanly.
# conflicts.py only threads a notes list through for these -- every other
# strategy keeps the plain two/three-argument call it always had.
NEEDS_NOTES = frozenset({"esd-3way", "param-rows"})
# Strategies that can be told the base they were handed is the installed
# game's own file. conflicts.py sets it on the one fold step that leads with
# a caller-supplied base, and never infers it from the data.
TAKES_GAME_BASE = frozenset({"param-rows"})
