"""Three-way merge of two mods' edits to one ESD state machine.

Group-level for everything that can be settled there, which measured against the two
real mods is all but one group out of 86. A group both sides changed needs the state
alignment in `align.py`'s sense -- see `_merge_group`.
"""
from .errors import ErmError
from .formats import esd


class EsdMergeError(ErmError):
    """Two ESDs could not be composed without dropping someone's work."""


def _by_id(archive):
    return {g.id: g for g in archive.groups}


def _same(a, b):
    """Whether two groups hold the same machine, ignoring layout bookkeeping."""
    return _shape(a) == _shape(b)


def _shape(group):
    if group is None:
        return None
    return tuple(
        (s.id, tuple(_cond_shape(c) for c in s.conditions),
         tuple(_call_shape(c) for c in s.entry),
         tuple(_call_shape(c) for c in s.exit),
         tuple(_call_shape(c) for c in s.while_))
        for s in group.states)


def _cond_shape(cond):
    return (cond.target, cond.evaluator,
            tuple(_call_shape(c) for c in cond.pass_commands),
            tuple(_cond_shape(c) for c in cond.subconditions))


def _call_shape(call):
    return (call.bank, call.id, tuple(a.bytecode for a in call.args))


def _degraft_call(call):
    return call._replace(
        order=None,
        args=tuple(a._replace(order=None, blob_order=None) for a in call.args))


def _degraft_condition(cond):
    return cond._replace(
        order=None, blob_order=None,
        pass_commands=tuple(_degraft_call(c) for c in cond.pass_commands),
        subconditions=tuple(_degraft_condition(c) for c in cond.subconditions))


def _degraft_state(state):
    return state._replace(
        order=None,
        conditions=tuple(_degraft_condition(c) for c in state.conditions),
        entry=tuple(_degraft_call(c) for c in state.entry),
        exit=tuple(_degraft_call(c) for c in state.exit),
        while_=tuple(_degraft_call(c) for c in state.while_))


def _degraft_group(group):
    """Clear `order`/`blob_order` on every record reachable from `group`, recursively.

    `order` is only unique within the file a group's records were read from (see
    esd.py's `_dedupe`). A group taken wholesale from `other` carries `other`'s
    table indices, which collide with `base`'s by coincidence of position, not
    identity -- esd.write's dedupe guard raises on exactly that collision. Clearing
    to None marks every record as freshly created, so the writer appends it instead
    of trying to slot it back into a table position that means something different
    in the merged file.
    """
    return group._replace(
        order=None, states=tuple(_degraft_state(s) for s in group.states))


def merge(base, other, vanilla):
    """Compose `other`'s edits onto `base`. Returns (merged, notes)."""
    b, o, v = _by_id(base), _by_id(other), _by_id(vanilla)
    notes, out = [], []

    for gid in sorted(set(b) | set(o) | set(v)):
        bg, og, vg = b.get(gid), o.get(gid), v.get(gid)
        other_changed = not _same(og, vg)
        base_changed = not _same(bg, vg)
        if not other_changed:
            if bg is not None:               # base's own edit, or untouched vanilla
                out.append(bg)
            continue
        if not base_changed:
            if og is not None:               # only the other side moved
                out.append(_degraft_group(og))
            continue
        if og is None and bg is None:
            continue                          # both deleted it
        if _same(bg, og):
            out.append(bg)                    # same edit, both sides
            continue
        merged, group_notes = _merge_group(gid, bg, og, vg)
        out.append(merged)
        notes.extend(group_notes)

    ordered = sorted((g for g in out if g is not None),
                     key=lambda g: (g.order is None, g.order if g.order is not None else 0))
    return base._replace(groups=tuple(ordered)), notes


def _merge_group(gid, base_group, other_group, vanilla_group):
    raise EsdMergeError(
        f"state group {gid} was modified by both mods and cannot be composed yet")
