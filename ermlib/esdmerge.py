"""Three-way merge of two mods' edits to one ESD state machine.

Group-level for everything that can be settled there, which measured against the two
real mods is all but one group out of 86. A group both sides changed needs `align`
to pair its states up first -- see `_merge_group`.

Telling an edit from a re-encode is the hard part, and `canonical` is what makes it
possible: three encoders wrote these files and they spell the same machine three
different ways.
"""
import struct
from typing import NamedTuple

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


# --- bytecode ---------------------------------------------------------------
#
# A condition's evaluator -- and a command argument -- is a postfix stack
# program. Three encoders wrote the files this merge has to compare: FromSoft's
# compiler, and the toolchain behind each mod. They agree on the machine and
# disagree on almost everything about how to spell it, so the only way to tell
# an edit from a re-encode is to decode both sides and compare the expressions.
#
# The opcode table below is not documented anywhere. It was recovered by
# decoding all 11038 blobs in the three real files and requiring every one to
# consume its bytes exactly and leave exactly one value on the stack at its
# terminator. Nothing else fits; a table off by one anywhere fails hundreds of
# blobs.
_END = 0xA1
_IMMEDIATE = {0x80: ("<f", 4), 0x81: ("<d", 8), 0x82: ("<i", 4)}
_CALL = range(0x84, 0x8C)      # pops (op - 0x83) values: function id, then args
_BINARY = range(0x8C, 0x9C)
_MARKER = (0xA6, 0xB7)         # FromSoft-only; leaves the value stack untouched
_SAVE = range(0xA7, 0xAF)      # cache the value on top in slot (op - 0xA7)
_LOAD = range(0xAF, 0xB7)      # push slot (op - 0xAF) back on
_ONE_ARG_BUILTIN = 0xB8
_NO_ARG_BUILTINS = (0xB9, 0xBA)
_EQ, _NE, _AND, _OR = 0x95, 0x96, 0x98, 0x99
_TRUE = ("int", 1)


def _expression(bytecode, slots):
    """Decode one blob into a comparable expression tree.

    `slots` is the register file, shared across everything in one state:
    FromSoft's compiler computes a subexpression once, parks it, and reloads it
    from a later condition of the same state.
    """
    stack, at = [], 0
    while at < len(bytecode):
        op = bytecode[at]
        at += 1
        if op == _END:
            if at != len(bytecode) or len(stack) != 1:
                raise EsdMergeError(
                    f"bytecode {bytecode.hex()} is not one expression -- it "
                    f"ends with {len(stack)} values on the stack")
            return stack[0]
        if op <= 0x7F:
            stack.append(("int", op - 0x40))       # 0x40 is zero; 0x3F is -1
        elif op in _IMMEDIATE:
            fmt, size = _IMMEDIATE[op]
            if at + size > len(bytecode):
                raise EsdMergeError(
                    f"bytecode {bytecode.hex()} is cut off mid-literal")
            kind = "int" if op == 0x82 else "float"
            stack.append((kind, struct.unpack_from(fmt, bytecode, at)[0]))
            at += size
        elif op in _CALL:
            stack.append(("call", _take(stack, op - 0x83, bytecode)))
        elif op in _BINARY:
            lhs, rhs = _take(stack, 2, bytecode)
            stack.append(_combine(op, lhs, rhs))
        elif op == _ONE_ARG_BUILTIN:
            stack.append(("builtin", op, _take(stack, 1, bytecode)))
        elif op in _NO_ARG_BUILTINS:
            stack.append(("builtin", op, ()))
        elif op in _MARKER:
            pass
        elif op in _SAVE:
            cached = _take(stack, 1, bytecode)[0]     # inspects, does not consume
            slots[op - _SAVE.start] = cached
            stack.append(cached)
        elif op in _LOAD:
            slot = op - _LOAD.start
            if slot not in slots:
                raise EsdMergeError(
                    f"bytecode {bytecode.hex()} reads cache slot {slot} before "
                    f"anything in this state wrote it")
            stack.append(slots[slot])
        else:
            raise EsdMergeError(
                f"bytecode {bytecode.hex()} uses opcode {op:#04x}, which this "
                f"decoder has never seen -- refusing to guess what it means")
    raise EsdMergeError(f"bytecode {bytecode.hex()} has no terminator")


def _take(stack, count, bytecode):
    if len(stack) < count:
        raise EsdMergeError(
            f"bytecode {bytecode.hex()} pops {count} values off a stack of "
            f"{len(stack)}")
    taken = tuple(stack[len(stack) - count:])
    del stack[len(stack) - count:]
    return taken


def _combine(op, lhs, rhs):
    """Build a binary node, folding the spellings the encoders disagree about."""
    if op in (_EQ, _NE) and _is_literal(lhs) and not _is_literal(rhs):
        lhs, rhs = rhs, lhs               # constant goes on the right
    if op == _EQ and rhs == _TRUE:
        return lhs                        # `X == 1` is `X`
    if op == _AND and rhs == _TRUE:
        return lhs
    if op == _AND and lhs == _TRUE:
        return rhs
    if op in (_AND, _OR):
        # Associative, so keep chains flat -- a run of same-target branches
        # merges into one of these and the grouping must not matter.
        return ("chain", op) + _operands(op, lhs) + _operands(op, rhs)
    return ("op", op, lhs, rhs)


def _is_literal(node):
    return node[0] in ("int", "float")


def _operands(op, node):
    return node[2:] if node[:2] == ("chain", op) else (node,)


# --- comparable states ------------------------------------------------------


def canonical(state):
    """A comparable form of a state, with the encoders' re-spellings removed.

    Compare raw structure instead and 46 groups that neither mod touched read
    as modified, because every mod tool rewrites machines it never edited. What
    survives here is the state's meaning: which condition jumps where, under
    which expression, running which commands.
    """
    slots = {}
    return (state.id,
            _canonical_conditions(state.conditions, slots),
            tuple(_canonical_call(c, slots) for c in state.entry),
            tuple(_canonical_call(c, slots) for c in state.exit),
            tuple(_canonical_call(c, slots) for c in state.while_))


def _canonical_conditions(conditions, slots):
    out = []
    for condition in conditions:
        item = _canonical_condition(condition, slots)
        if out and _one_branch_twice(out[-1], item):
            target, evaluator = out[-1][0], out[-1][1]
            out[-1] = (target, _combine(_OR, evaluator, item[1]), (), ())
        else:
            out.append(item)
    return tuple(out)


def _one_branch_twice(first, second):
    """Whether two adjacent conditions are one branch written apart.

    Same jump target, nothing else attached to either: taking the first is
    indistinguishable from taking the second, so `IF A -> T; IF B -> T` and
    `IF A || B -> T` are the same machine. One toolchain writes them merged.
    """
    target, _, passes, subconditions = first
    other_target, _, other_passes, other_subconditions = second
    return (target is not None and target == other_target
            and not passes and not subconditions
            and not other_passes and not other_subconditions)


def _canonical_condition(condition, slots):
    evaluator = _expression(condition.evaluator, slots) if condition.evaluator else None
    passes = tuple(_canonical_call(c, slots) for c in condition.pass_commands)
    subconditions = _canonical_conditions(condition.subconditions, slots)
    target = condition.target
    if target is None and not passes and len(subconditions) == 1:
        # `IF E: { IF F -> T }` is `IF E && F -> T`, and vanilla writes the
        # nested form where both mods write the flat one. Only sound while the
        # outer condition runs no commands: those fire when E holds and F does
        # not, which the flattened form would never do.
        child_target, child_evaluator, child_passes, child_subconditions = subconditions[0]
        if not child_subconditions and None not in (evaluator, child_evaluator):
            target = child_target
            evaluator = _combine(_AND, evaluator, child_evaluator)
            passes, subconditions = child_passes, ()
    return (target, evaluator, passes, subconditions)


def _canonical_call(call, slots):
    return (call.bank, call.id,
            tuple(_expression(a.bytecode, slots) if a.bytecode else None
                  for a in call.args))


_UNSETTLED = ("unsettled",)     # a jump whose far end has not been paired yet


class Alignment(NamedTuple):
    """How one group's states line up with another's.

    `pairs` is the answer. The rest is why a state is not in it, and the two
    reasons are not the same: `left_only`/`right_only` are states with no
    counterpart left to have -- a real delete, a real add -- while `unresolved`
    is this function admitting defeat, with candidates it could not tell apart.
    A caller replaying a delta has to treat those differently, so a state never
    just goes missing from the dict.

    The right-hand states behind `unresolved` are the ones in neither
    `pairs.values()` nor `right_only`.
    """
    pairs: dict
    left_only: tuple
    right_only: tuple
    unresolved: tuple


def align(left, right):
    """Line `left`'s states up with `right`'s, by content rather than by id.

    Boss Res deletes states from the group both mods edit and renumbers the
    survivors densely, so the same id means different things on the two sides
    and matching on it pairs unrelated machines.

    Content on its own is not enough either, because a state's content includes
    where its conditions jump and those are exactly the ids a renumbering
    moves. Vanilla state 3 is `IF 1 -> 32`; Boss Res renumbered 32 to 30, so
    the two stop matching and a different trampoline still pointing at 32 wins
    the state instead. Identity depends on the targets and the targets' identity
    depends on the mapping, which makes it a fixed point: seed on the part of a
    state a renumbering cannot touch, then re-read the jump targets through
    whatever is settled and go round again until a round settles nothing new.
    """
    left_shape = {s.id: canonical(s) for s in left.states}
    right_shape = {s.id: canonical(s) for s in right.states}
    pairs, taken = {}, set()

    def settle(from_left, from_right, by_id=False):
        """Add every pair this round can justify; answer how many that was."""
        added = 0
        if by_id:
            for sid in sorted(left_shape):
                if (sid not in pairs and sid in right_shape and sid not in taken
                        and _retargeted(left_shape[sid], from_left)
                        == _retargeted(right_shape[sid], from_right)):
                    pairs[sid] = sid
                    taken.add(sid)
                    added += 1
        # Only a candidate that is alone on both sides is safe to take: two
        # states with the same content are interchangeable until something
        # downstream of them settles and tells them apart.
        lefts, rights = {}, {}
        for sid in sorted(left_shape):
            if sid not in pairs:
                lefts.setdefault(_retargeted(left_shape[sid], from_left), []).append(sid)
        for sid in sorted(right_shape):
            if sid not in taken:
                rights.setdefault(_retargeted(right_shape[sid], from_right), []).append(sid)
        for shape, candidates in lefts.items():
            twins = rights.get(shape, ())
            if len(candidates) == 1 and len(twins) == 1:
                pairs[candidates[0]] = twins[0]
                taken.add(twins[0])
                added += 1
        return added

    settle(_target_masked, _target_masked, by_id=True)
    through_pairs = lambda t: None if t is None else pairs.get(t, _UNSETTLED)
    already_taken = lambda t: None if t is None else (t if t in taken else _UNSETTLED)
    while settle(through_pairs, already_taken):
        pass

    return Alignment(pairs, *_leftovers(left_shape, right_shape, pairs, taken))


def _leftovers(left_shape, right_shape, pairs, taken):
    """Split what went unpaired into "nothing it could have been" and "could
    not choose", measured against the states still going spare."""
    free = lambda shapes, used: {_retargeted(s, _target_masked)
                                 for i, s in shapes.items() if i not in used}
    spare_right, spare_left = free(right_shape, taken), free(left_shape, pairs)
    left_only, unresolved = [], []
    for sid in sorted(left_shape):
        if sid in pairs:
            continue
        possible = _retargeted(left_shape[sid], _target_masked) in spare_right
        (unresolved if possible else left_only).append(sid)
    right_only = tuple(
        sid for sid in sorted(right_shape)
        if sid not in taken
        and _retargeted(right_shape[sid], _target_masked) not in spare_left)
    return tuple(left_only), right_only, tuple(unresolved)


def _target_masked(target):
    """Every jump looks the same -- the seed round has no mapping to read them
    through yet, and their raw ids are the thing that moved."""
    return None if target is None else _UNSETTLED


def _retargeted(shape, translate):
    """A state's canonical form minus its own id, with jump targets rewritten.

    Dropping the id is what lets a renumbered state match at all; rewriting the
    targets is what stops the ids buried inside it doing the same damage.
    """
    _sid, conditions, entry, exit_, while_ = shape
    return (_conditions_retargeted(conditions, translate), entry, exit_, while_)


def _conditions_retargeted(conditions, translate):
    return tuple(
        (translate(target), evaluator, passes,
         _conditions_retargeted(subconditions, translate))
        for target, evaluator, passes, subconditions in conditions)
