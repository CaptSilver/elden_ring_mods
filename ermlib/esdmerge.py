"""Three-way merge of two mods' edits to one ESD state machine.

Group-level for everything that can be settled there, which measured against the two
real mods is all but one group out of 86. A group both sides changed needs `align`
to pair its states up first -- see `_merge_group`.

Telling an edit from a re-encode is the hard part, and `canonical` is what makes it
possible: three encoders wrote these files and they spell the same machine three
different ways.
"""
import struct
from enum import Enum
from typing import NamedTuple, Optional

from .errors import ErmError
from .formats import esd


class EsdMergeError(ErmError):
    """Two ESDs could not be composed without dropping someone's work."""


class Reason(Enum):
    """Why one thing in a shared group's merge could not be carried over as-is."""
    UNRESOLVED_IN_OTHER = "unresolved-in-other"    # several of other's states look like this vanilla state
    NOT_IN_OTHER = "not-in-other"                  # other has no state matching this vanilla state
    NOT_IN_EITHER = "not-in-either"                # neither side has one, so no copy survived
    UNRESOLVED_IN_BASE = "unresolved-in-base"      # several of base's states look like this vanilla state
    NOT_IN_BASE = "not-in-base"                    # base deleted this vanilla state
    BOTH_CHANGED = "both-changed"                  # both sides edited it differently; base's version was kept
    UNPLACEABLE_JUMP = "unplaceable-jump"          # other's edit jumps somewhere with no counterpart in the merge
    ORPHANED_GRAFT = "orphaned-graft"              # other's added state arrived with nothing reaching it


class Note(NamedTuple):
    """One thing a group's merge could not carry over cleanly.

    `state` is a vanilla state id for every reason except ORPHANED_GRAFT, where
    there is no vanilla state to name at all -- it's the other mod's own id for a
    state it added. `target` only carries meaning for two reasons: the jump target
    that had no home in the merge (UNPLACEABLE_JUMP), and the id a graft landed at
    (ORPHANED_GRAFT).

    Kept structural rather than a formatted string: `esdmerge` decides *that* a
    merge could not carry something across and *why*, and describe() below is the
    only place that turns it into English -- tests assert on these fields instead
    of grepping prose, and the wording can change without touching either.
    """
    group: int
    state: int
    reason: Reason
    target: Optional[int] = None


_PROSE = {
    Reason.UNRESOLVED_IN_OTHER: (
        "group {group}: several of the other mod's states look like vanilla "
        "state {state} and nothing separates them, so any change it made there "
        "was not applied"),
    Reason.NOT_IN_OTHER: (
        "group {group}: the other mod has no state matching vanilla state "
        "{state} -- it either removed or rewrote it, and the preferred mod's "
        "copy was kept"),
    Reason.NOT_IN_EITHER: (
        "group {group}: neither mod has a state matching vanilla state {state} "
        "-- both removed or rewrote it, so nothing of it survived the merge"),
    Reason.UNRESOLVED_IN_BASE: (
        "group {group}: the other mod changed vanilla state {state}, and "
        "several of the preferred mod's states look like it -- the change was "
        "not applied rather than applied to the wrong one"),
    Reason.NOT_IN_BASE: (
        "group {group}: the other mod changed vanilla state {state}, which the "
        "preferred mod deleted -- that change was not applied"),
    Reason.BOTH_CHANGED: (
        "group {group}: both mods changed vanilla state {state} -- kept the "
        "preferred mod's version"),
    Reason.UNPLACEABLE_JUMP: (
        "group {group}: the other mod's change to vanilla state {state} jumps "
        "to its state {target}, which has no counterpart in the merged group -- "
        "that change was not applied"),
    Reason.ORPHANED_GRAFT: (
        "group {group}: the other mod's state {state} came across as state "
        "{target}, but nothing in the merged group reaches it -- whatever it "
        "does will not run"),
}


def describe(note):
    """The English sentence an apply report shows for one merge note."""
    return _PROSE[note.reason].format(group=note.group, state=note.state, target=note.target)


def _by_id(archive):
    return {g.id: g for g in archive.groups}


def _same(a, b):
    """Whether two groups hold the same machine, ignoring how it was spelled.

    Comparing the raw structure instead reads 46 groups neither mod touched as
    modified, because every mod tool rewrites machines it never edited -- and a
    group that reads as modified on both sides goes to `_merge_group`, which
    would then be composing deltas nobody authored. `canonical` is what makes
    the difference between an edit and a re-encode visible.

    A missing group is not canonicalised at all: there is nothing to compare it
    against, and a group only one side has may hold bytecode this module cannot
    decode without that being anybody's problem.
    """
    if a is None or b is None:
        return a is None and b is None
    return _shape(a) == _shape(b)


def _shape(group):
    return tuple(canonical(s) for s in group.states)


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
    composed = base._replace(groups=tuple(ordered))
    check(composed)
    return composed, notes


class _Unplaceable(Exception):
    """A jump target in `other`'s numbering with no counterpart in the merge."""

    def __init__(self, target):
        super().__init__(target)
        self.target = target


def _merge_group(gid, base_group, other_group, vanilla_group):
    """Replay `other`'s edits to one group onto `base`'s version of it.

    Neither side's state ids can be trusted as identities here -- one mod deletes
    states and renumbers the survivors -- so everything goes through an alignment
    against the vanilla both sides branched from. `other`'s delta is read against
    vanilla and written out in `base`'s numbering.

    Anything that cannot be replayed is named in the returned notes rather than
    dropped. That is the whole point: a hook that quietly fails to land leaves a
    mod installed, loading, and doing nothing.
    """
    # Three different things get here, and they are three different problems to
    # go and fix. Boss Res already deletes group 2147483563 outright, so the
    # deletion arms are not hypothetical -- the next mod that touches that group
    # lands on one of them, and the message is all it gets.
    if vanilla_group is None:
        raise EsdMergeError(
            f"state group {gid} was added by both mods, with different contents "
            f"-- there is no vanilla version to read either one as an edit of, "
            f"so there is no delta to replay")
    if base_group is None:
        raise EsdMergeError(
            f"state group {gid} was deleted by the preferred mod and changed by "
            f"the other -- there is nothing left to replay that change onto")
    if other_group is None:
        raise EsdMergeError(
            f"state group {gid} was deleted by the other mod and changed by the "
            f"preferred mod -- keeping the deletion would drop the preferred "
            f"mod's change and keeping the change would undo the deletion")

    to_base = align(vanilla_group, base_group)
    to_other = align(vanilla_group, other_group)
    vanilla_states = {s.id: s for s in vanilla_group.states}
    base_states = {s.id: s for s in base_group.states}
    other_states = {s.id: s for s in other_group.states}
    other_to_vanilla = {right: left for left, right in to_other.pairs.items()}

    notes = []
    merged = dict(base_states)
    layout = [s.id for s in base_group.states]

    # A state `other` holds that vanilla never did is an addition to graft. That
    # is `right_only` and not "everything unpaired": an unpaired state may also
    # be one the alignment could not place, and grafting one of those adds a
    # second copy of a machine already sitting in the group.
    #
    # Ids are only unique within one file, so a graft keeps its own number only
    # where base is not already using it -- and every reference to it has to move
    # with it, which is why the whole allocation happens before anything is
    # retargeted.
    additions = to_other.right_only
    used = set(base_states)
    graft_id = {}
    for sid in additions:
        graft_id[sid] = sid if sid not in used else max(used, default=-1) + 1
        used.add(graft_id[sid])

    def retarget(target):
        """An id in `other`'s numbering, as an id in the merged group."""
        if target is None:
            return None
        if target in graft_id:
            return graft_id[target]
        van_id = other_to_vanilla.get(target)
        settled = to_base.pairs.get(van_id) if van_id is not None else None
        if settled is None:
            raise _Unplaceable(target)
        return settled

    def replay(state, sid, order):
        """`other`'s state, renumbered into the merged group and detached from
        `other`'s record tables. Commands come across with the conditions: a
        state's meaning is not only where it branches."""
        fresh = _degraft_state(state)
        return fresh._replace(
            id=sid, order=order,
            conditions=_retargeted_conditions(fresh.conditions, retarget))

    for sid in additions:
        try:
            merged[graft_id[sid]] = replay(other_states[sid], graft_id[sid], None)
        except _Unplaceable as jump:
            raise EsdMergeError(
                f"group {gid}: the other mod's new state {sid} jumps to its state "
                f"{jump.target}, which has no counterpart in the merged group")
        layout.append(graft_id[sid])

    for van_id in sorted(vanilla_states):
        if van_id in to_other.unresolved:
            notes.append(Note(gid, van_id, Reason.UNRESOLVED_IN_OTHER))
            continue
        other_id = to_other.pairs.get(van_id)
        if other_id is None:
            # "the preferred mod's copy was kept" is only true if there is one.
            # `left_only` is the alignment saying base has nothing this could
            # be, as opposed to `unresolved`, where some copy of it does ship.
            notes.append(Note(gid, van_id, Reason.NOT_IN_EITHER
                              if van_id in to_base.left_only else Reason.NOT_IN_OTHER))
            continue
        if not _edited(vanilla_states[van_id], other_states[other_id], to_other.pairs):
            continue
        if van_id in to_base.unresolved:
            notes.append(Note(gid, van_id, Reason.UNRESOLVED_IN_BASE))
            continue
        base_id = to_base.pairs.get(van_id)
        if base_id is None:
            notes.append(Note(gid, van_id, Reason.NOT_IN_BASE))
            continue
        if _edited(vanilla_states[van_id], base_states[base_id], to_base.pairs):
            notes.append(Note(gid, van_id, Reason.BOTH_CHANGED))
            continue
        try:
            merged[base_id] = replay(other_states[other_id], base_id,
                                     base_states[base_id].order)
        except _Unplaceable as jump:
            notes.append(Note(gid, van_id, Reason.UNPLACEABLE_JUMP, target=jump.target))

    composed = base_group._replace(states=tuple(merged[sid] for sid in layout))
    for sid in _orphaned_grafts(other_group, composed, graft_id):
        notes.append(Note(gid, sid, Reason.ORPHANED_GRAFT, target=graft_id[sid]))
    return composed, notes


def _edited(before, after, pairs):
    """Whether `after` is a changed version of `before`, read through `pairs`.

    A raw comparison is no good: the mod that renumbered its states rewrote
    every jump target along with them, so an untouched state stops matching
    itself. Targets on the vanilla side are translated into the mod's numbering
    first, and one the alignment could not place reads as a difference rather
    than risking a false match.
    """
    through = lambda target: None if target is None else pairs.get(target, _UNSETTLED)
    return (_retargeted(canonical(before), through)
            != _retargeted(canonical(after), lambda target: target))


def _retargeted_conditions(conditions, retarget):
    return tuple(
        c._replace(target=retarget(c.target),
                   subconditions=_retargeted_conditions(c.subconditions, retarget))
        for c in conditions)


def _orphaned_grafts(other_group, merged_group, graft_id):
    """States the other mod could enter, and the merged group cannot.

    Deliberately relative, not "every state is reachable": shipped files are
    full of states nothing jumps at -- 22 of vanilla's, across 10 of its 86
    groups, and the same in both mods -- so an absolute rule would reject
    FromSoft's own work. What is not normal is a machine the other mod added
    *and could enter* arriving with nothing left pointing at it, which is what
    happens when the hook that would have entered it loses a conflict.

    The result is a note rather than a refusal. Base's mod still works, and a
    merge that keeps one mod and names what the other lost beats aborting and
    shipping neither.
    """
    live = _reachable(other_group)
    arrived = _reachable(merged_group)
    return tuple(sid for sid in sorted(graft_id)
                 if sid in live and graft_id[sid] not in arrived)


def _reachable(group):
    """State ids reachable from the group's entry.

    The entry is the group's first state row -- what its header points at. Every
    group in all three real files starts at state 0.
    """
    states = {s.id: s for s in group.states}
    start = group.states[0].id
    seen, pending = {start}, [start]
    while pending:
        for target in _jump_targets(states[pending.pop()]):
            if target in states and target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def _jump_targets(state):
    return tuple(c.target for c in _every_condition(state.conditions)
                 if c.target is not None)


def check(archive):
    """Structural invariants for a merged graph.

    The ESD analogue of checking that every authored byte survived a param
    merge: a graft that renumbers wrongly shows up here as a dangling target,
    rather than as a dead grace menu three hours into a playthrough.
    """
    group_ids = {g.id for g in archive.groups}
    for group in archive.groups:
        ids = [s.id for s in group.states]
        present = set(ids)
        if len(ids) != len(present):
            repeated = sorted(i for i in present if ids.count(i) > 1)
            raise EsdMergeError(
                f"state group {group.id} has more than one state numbered "
                f"{', '.join(str(i) for i in repeated)}")
        for state in group.states:
            where = f"group {group.id} state {state.id}"
            calls = list(state.entry + state.exit + state.while_)
            for condition in _every_condition(state.conditions):
                if condition.target is not None and condition.target not in present:
                    raise EsdMergeError(
                        f"{where} jumps to state {condition.target}, which does "
                        f"not exist in that group")
                calls.extend(condition.pass_commands)
            for call in calls:
                if call.bank == esd.CALL_BANK and call.id not in group_ids:
                    raise EsdMergeError(
                        f"{where} calls machine {call.id}, which is not in the "
                        f"merged file")


def _every_condition(conditions):
    for condition in conditions:
        yield condition
        yield from _every_condition(condition.subconditions)


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
