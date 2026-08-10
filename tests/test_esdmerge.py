import pytest

from ermlib import esdmerge
from ermlib.formats import esd
from tests.esd_fixtures import real_esd

# Real bytecode, in the spellings the three files actually use. A condition
# evaluator is a postfix stack program: 0x40+n pushes a small int, 0x82 pushes
# an int32, 0x80/0x81 push a float32/float64, 0xB8/0xB9/0xBA are builtins,
# 0x95/0x96/0x98/0x99 are == != && ||, and 0xA1 terminates it.

ALWAYS = b"\x41\xa1"                          # push 1 -- FromSoft's "IF 1"
ALWAYS_INT32 = b"\x82\x01\x00\x00\x00\xa1"    # both mods' spelling of the same
ALWAYS_EQ_ONE = b"\x41\x82\x01\x00\x00\x00\x95\xa1"     # and a third: 1 == 1
GUARD = b"\xb9\xba\x96\xa1"                   # b9 != ba
OTHER = b"\x4f\xb8\xa1"                       # b8(15)
GUARD_AND_OTHER = b"\xb9\xba\x96\x4f\xb8\x98\xa1"
GUARD_OR_OTHER = b"\xb9\xba\x96\x4f\xb8\x99\xa1"
GUARD_OR_GUARD = b"\xb9\xba\x96\xb9\xba\x96\x99\xa1"


def _esd(groups):
    """groups: {group_id: [(state_id, target_or_None)]}"""
    return esd.Esd(
        groups=tuple(
            esd.StateGroup(gid, tuple(
                esd.State(sid, conditions=() if target is None else
                          (esd.Condition(target=target, evaluator=ALWAYS),))
                for sid, target in states))
            for gid, states in sorted(groups.items())),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)


def _ids(archive):
    return sorted(g.id for g in archive.groups)


def _group(archive, gid):
    return next(g for g in archive.groups if g.id == gid)


def _find(group, sid):
    return next(s for s in group.states if s.id == sid)


def _targets(state):
    out = []

    def walk(conditions):
        for condition in conditions:
            if condition.target is not None:
                out.append(condition.target)
            walk(condition.subconditions)

    walk(state.conditions)
    return out


def _reachable(group, start=0):
    states = {s.id: s for s in group.states}
    seen, pending = {start}, [start]
    while pending:
        for target in _targets(states[pending.pop()]):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def test_a_group_only_the_other_side_added_is_grafted():
    vanilla = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)]})
    other = _esd({1: [(0, None)], 1000: [(0, None), (1, 0)]})
    merged, notes = esdmerge.merge(base, other, vanilla)
    assert _ids(merged) == [1, 1000]
    # The group's contents, not just its id: a graft that arrives as an empty
    # shell of the right number is the failure this is guarding against.
    grafted = _group(merged, 1000)
    assert [s.id for s in grafted.states] == [0, 1]
    assert _targets(_find(grafted, 1)) == [0]
    assert notes == []


def test_a_group_the_base_added_survives_the_merge():
    """The three-way is what stops the other side's untouched copy reverting work
    the base did. A two-way union would drop these."""
    vanilla = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)], 88: [(0, None)]})
    other = _esd({1: [(0, None)]})
    merged, _ = esdmerge.merge(base, other, vanilla)
    assert _ids(merged) == [1, 88]


def test_a_group_the_other_side_deleted_is_deleted():
    vanilla = _esd({1: [(0, None)], 85: [(0, None)]})
    base = _esd({1: [(0, None)], 85: [(0, None)]})
    other = _esd({1: [(0, None)]})
    merged, _ = esdmerge.merge(base, other, vanilla)
    assert _ids(merged) == [1]


def test_a_group_only_the_other_side_modified_is_taken():
    vanilla = _esd({1: [(0, None)], 32: [(0, None)]})
    base = _esd({1: [(0, None)], 32: [(0, None)]})
    other = _esd({1: [(0, None)], 32: [(0, None), (1, 0)]})
    merged, _ = esdmerge.merge(base, other, vanilla)
    group = next(g for g in merged.groups if g.id == 32)
    assert [s.id for s in group.states] == [0, 1]


def test_a_group_only_the_base_modified_keeps_the_base_version():
    vanilla = _esd({1: [(0, None)], 32: [(0, None)]})
    base = _esd({1: [(0, None)], 32: [(0, None), (1, 0)]})
    other = _esd({1: [(0, None)], 32: [(0, None)]})
    merged, _ = esdmerge.merge(base, other, vanilla)
    group = next(g for g in merged.groups if g.id == 32)
    assert [s.id for s in group.states] == [0, 1]


def test_a_group_both_sides_invented_differently_is_refused():
    """No vanilla to express either side's edit against, so there is no delta to
    replay -- the two machines are simply different machines under one id."""
    vanilla = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)], 24: [(0, None), (1, 0)]})
    other = _esd({1: [(0, None)], 24: [(0, None), (2, 0)]})
    with pytest.raises(esdmerge.EsdMergeError, match="added by both mods"):
        esdmerge.merge(base, other, vanilla)


def test_a_group_the_base_deleted_and_the_other_changed_says_which_happened():
    """Boss Res deletes group 2147483563 outright, so this arm is one mod away
    from firing for real. "added or deleted by both mods differently" would
    send someone looking for an addition that isn't there."""
    vanilla = _esd({1: [(0, None)], 85: [(0, 1), (1, None)]})
    base = _esd({1: [(0, None)]})                                 # deleted 85
    other = _esd({1: [(0, None)], 85: [(0, None), (1, None)]})    # changed it
    with pytest.raises(esdmerge.EsdMergeError,
                       match="deleted by the preferred mod and changed by the other"):
        esdmerge.merge(base, other, vanilla)


def test_a_group_the_other_deleted_and_the_base_changed_says_which_happened():
    """The mirror image, and a different thing to go fix: here it is the other
    mod's deletion that has to be argued with, not the preferred mod's."""
    vanilla = _esd({1: [(0, None)], 85: [(0, 1), (1, None)]})
    base = _esd({1: [(0, None)], 85: [(0, None), (1, None)]})     # changed it
    other = _esd({1: [(0, None)]})                                # deleted 85
    with pytest.raises(esdmerge.EsdMergeError,
                       match="deleted by the other mod and changed by the preferred"):
        esdmerge.merge(base, other, vanilla)


def test_a_group_both_mods_deleted_stays_deleted():
    """Agreement, not a conflict -- and it must not reach the group merge, which
    has no version of the group to work from at all."""
    vanilla = _esd({1: [(0, None)], 85: [(0, None)]})
    base = _esd({1: [(0, None)]})
    other = _esd({1: [(0, None)]})
    merged, notes = esdmerge.merge(base, other, vanilla)
    assert _ids(merged) == [1]
    assert notes == []


def test_a_group_both_mods_added_identically_is_taken_once():
    """Two mods shipping the same new machine is the other half of the
    both-added case, and the half that is not a conflict."""
    vanilla = _esd({1: [(0, None)]})
    added = {1: [(0, None)], 1000: [(0, 1), (1, None)]}
    merged, notes = esdmerge.merge(_esd(added), _esd(added), vanilla)
    assert _ids(merged) == [1, 1000]
    assert [s.id for s in _group(merged, 1000).states] == [0, 1]
    assert notes == []


def test_a_grafted_group_carries_no_leftover_order_and_survives_write():
    """The real check: a merge whose output cannot serialise is not a merge.

    other's group 1000 carries order/blob_order stamped from other's own read.
    Reusing them wholesale would collide with base's own record orders (both
    files independently number their tables from 0) and esd.write's dedupe
    guard would raise EsdError on the collision. Every record in a grafted
    group -- state, condition, and nested command calls/args -- has to come
    out with order (and blob_order) cleared to None so it's treated as a
    fresh append instead.
    """
    vanilla = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)]})
    other = esd.Esd(
        groups=(
            esd.StateGroup(1, (esd.State(0, order=0),), order=0),
            esd.StateGroup(1000, (
                esd.State(0, order=1, conditions=(
                    esd.Condition(target=1, evaluator=ALWAYS, order=0,
                                  blob_order=0),
                )),
                esd.State(1, order=2, entry=(
                    esd.CommandCall(6, 1000, args=(
                        esd.CommandArg(b"\x40\xa1", order=0, blob_order=100),
                    ), order=0),
                )),
            ), order=1),
        ),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    merged, _ = esdmerge.merge(base, other, vanilla)
    grafted = next(g for g in merged.groups if g.id == 1000)

    # Every record reachable from the grafted group must have order (and
    # blob_order, where the field exists) cleared -- recursively, through
    # states, conditions, subconditions, command calls and command args.
    for state in grafted.states:
        assert state.order is None
        for cond in state.conditions:
            assert cond.order is None
            assert cond.blob_order is None
        for call in state.entry + state.exit + state.while_:
            assert call.order is None
            for arg in call.args:
                assert arg.order is None
                assert arg.blob_order is None

    data = esd.write(merged)
    round_tripped = esd.read(data)
    assert sorted(g.id for g in round_tripped.groups) == [1, 1000]


def _assert_call_degrafted(call):
    assert call.order is None
    for arg in call.args:
        assert arg.order is None
        assert arg.blob_order is None


def _assert_condition_degrafted(cond):
    assert cond.order is None
    assert cond.blob_order is None
    for call in cond.pass_commands:
        _assert_call_degrafted(call)
    for sub in cond.subconditions:
        _assert_condition_degrafted(sub)


def test_a_grafted_condition_degrafts_through_subconditions_and_pass_commands():
    """The previous write-survival test only ever gave the grafted group a
    condition with empty subconditions and pass_commands, so it never proved
    _degraft_condition's two recursive calls do anything -- a version that
    dropped both (just `cond._replace(order=None, blob_order=None)`, no
    recursion) still passed it.

    Here every nested record -- a subcondition, a pass_commands call, and
    that call's own arg -- carries an `.order` (and `.blob_order`, for the
    arg) that collides with a real record base already owns in the same
    table, but with different content. Left uncleared, any one of them is
    indistinguishable from a genuine cross-file collision and trips
    esd.write's dedupe guard; the explicit walk below catches the same gap
    directly, field by field, without depending on write() raising.
    """
    group1 = esd.StateGroup(1, (
        esd.State(0, order=0, conditions=(
            esd.Condition(target=None, evaluator=b"\x42\xa1", order=0, blob_order=0),
        ), entry=(
            esd.CommandCall(9, 9, args=(
                esd.CommandArg(b"\x43\xa1", order=0, blob_order=0),
            ), order=0),
        )),
    ), order=0)
    vanilla = esd.Esd(groups=(group1,), name="t000001000",
                      unk=(0, 0, 0, 0), pool_count=0)
    base = vanilla       # group 1 is untouched by other -> base's stamped
                         # order/blob_order values above are what land in
                         # the merged output, unmodified

    graft_arg = esd.CommandArg(b"\x44\xa1", order=0, blob_order=0)  # collides with base's arg
    graft_call = esd.CommandCall(1, 1, args=(graft_arg,), order=0)  # collides with base's call
    graft_sub = esd.Condition(target=None, evaluator=b"\x45\xa1",
                              order=0, blob_order=0)                # collides with base's cond
    graft_cond = esd.Condition(target=1, evaluator=b"\x46\xa1",
                               pass_commands=(graft_call,),
                               subconditions=(graft_sub,),
                               order=50, blob_order=50)
    other = esd.Esd(
        groups=(
            group1,
            esd.StateGroup(1000, (
                esd.State(0, order=1, conditions=(graft_cond,)),
                esd.State(1, order=2),
            ), order=1),
        ),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    merged, _ = esdmerge.merge(base, other, vanilla)
    grafted = next(g for g in merged.groups if g.id == 1000)

    for state in grafted.states:
        assert state.order is None
        for cond in state.conditions:
            _assert_condition_degrafted(cond)
        for call in state.entry + state.exit + state.while_:
            _assert_call_degrafted(call)

    data = esd.write(merged)
    round_tripped = esd.read(data)
    assert sorted(g.id for g in round_tripped.groups) == [1, 1000]


# --- canonicalisation -------------------------------------------------------


def _state(sid, conditions=()):
    return esd.State(sid, conditions=tuple(conditions))


def _cond(target, evaluator=GUARD, subs=(), passes=()):
    return esd.Condition(target=target, evaluator=evaluator,
                         subconditions=tuple(subs), pass_commands=tuple(passes))


def test_canonical_reads_both_spellings_of_a_literal_the_same_way():
    """FromSoft's compiler has a one-byte push for small ints; both mod
    toolchains always write the four-byte one. Same number either way."""
    assert esdmerge.canonical(_state(0, [_cond(5, ALWAYS)])) == \
        esdmerge.canonical(_state(0, [_cond(5, ALWAYS_INT32)]))


def test_canonical_widens_a_float_without_changing_it():
    """Vanilla group 2147483609 writes 1.5 as a float32; both mods promote it
    to a float64. The value is identical, so the machine is unchanged."""
    as_float32 = b"\x82\x67\x00\x00\x00\x84\x80\x00\x00\xc0\x3f\x94\xa1"
    as_float64 = b"\x82\x67\x00\x00\x00\x84\x81\x00\x00\x00\x00\x00\x00\xf8\x3f\x94\xa1"
    assert esdmerge.canonical(_state(0, [_cond(5, as_float32)])) == \
        esdmerge.canonical(_state(0, [_cond(5, as_float64)]))


def test_canonical_ignores_the_markers_vanilla_writes_between_operands():
    """0xA6 shows up 553 times in vanilla and never in either mod. It leaves
    the value stack alone, so it cannot be part of what a condition means."""
    with_marker = b"\x4f\xb8\xa6\x40\x95\xa1"
    without = b"\x4f\xb8\x40\x95\xa1"
    assert esdmerge.canonical(_state(0, [_cond(5, with_marker)])) == \
        esdmerge.canonical(_state(0, [_cond(5, without)]))


def test_canonical_expands_a_cached_subexpression():
    """Vanilla computes a subexpression once, parks it in a register (0xA7+n)
    and reloads it (0xAF+n) in later conditions of the same state; the mods
    spell it out every time. Group 2147483570 state 1 does exactly this."""
    cached = _state(0, [_cond(1, b"\x4f\xb8\xa7\x40\x95\xa1"),   # b8(15) -> slot 0
                        _cond(2, b"\xaf\x42\x95\xa1")])          # slot 0 == 2
    spelled = _state(0, [_cond(1, b"\x4f\xb8\x40\x95\xa1"),
                         _cond(2, b"\x4f\xb8\x42\x95\xa1")])
    assert esdmerge.canonical(cached) == esdmerge.canonical(spelled)


def test_canonical_reduces_x_equals_one_to_x():
    assert esdmerge.canonical(_state(0, [_cond(5, b"\x4f\xb8\x82\x01\x00\x00\x00\x95\xa1")]) ) == \
        esdmerge.canonical(_state(0, [_cond(5, OTHER)]))


def test_canonical_ignores_which_side_of_a_comparison_holds_the_constant():
    """Group 2147483629: vanilla writes `0 == f(...)`, Melina writes
    `f(...) == 0`."""
    literal_first = b"\x40\x4f\xb8\x95\xa1"
    literal_last = b"\x4f\xb8\x40\x95\xa1"
    assert esdmerge.canonical(_state(0, [_cond(5, literal_first)])) == \
        esdmerge.canonical(_state(0, [_cond(5, literal_last)]))


def test_canonical_collapses_a_trivial_wrapper():
    """`IF E: { IF 1 -> T }` and `IF E -> T` are the same machine. One writer
    uses one form and one the other, on groups nobody edited."""
    nested = _state(0, [_cond(None, GUARD, subs=[_cond(5, ALWAYS)])])
    flat = _state(0, [_cond(5, GUARD)])
    assert esdmerge.canonical(nested) == esdmerge.canonical(flat)


def test_canonical_flattens_a_guarded_wrapper_into_a_conjunction():
    """The general case of the same artifact: vanilla nests `IF E: { IF F -> T }`
    where both mods write the flattened `IF E && F -> T` (group 2147483633)."""
    nested = _state(0, [_cond(None, GUARD, subs=[_cond(5, OTHER)])])
    flat = _state(0, [_cond(5, GUARD_AND_OTHER)])
    assert esdmerge.canonical(nested) == esdmerge.canonical(flat)


def test_canonical_flattens_a_whole_chain_of_wrappers():
    deep = _state(0, [_cond(None, GUARD, subs=[_cond(None, OTHER, subs=[_cond(5, ALWAYS)])])])
    flat = _state(0, [_cond(5, GUARD_AND_OTHER)])
    assert esdmerge.canonical(deep) == esdmerge.canonical(flat)


def test_canonical_will_not_flatten_a_wrapper_that_runs_commands():
    """Pass commands on the outer condition fire when E holds and F does not,
    which the flattened form would never do. Collapsing that would both lose
    the commands and call a real edit no change."""
    call = esd.CommandCall(1, 2)
    with_call = _state(0, [_cond(None, GUARD, subs=[_cond(5, OTHER)], passes=[call])])
    without = _state(0, [_cond(None, GUARD, subs=[_cond(5, OTHER)])])
    assert esdmerge.canonical(with_call) != esdmerge.canonical(without)


def test_canonical_will_not_flatten_away_a_branching_subtree():
    """Flattening a wrapper whose child still branches would drop the
    grandchildren on the floor and report two different machines as one."""
    branching = _state(0, [_cond(None, GUARD, subs=[
        _cond(None, OTHER, subs=[_cond(5), _cond(6, OTHER)])])])
    empty = _state(0, [_cond(None, GUARD, subs=[_cond(None, OTHER)])])
    assert esdmerge.canonical(branching) != esdmerge.canonical(empty)


def test_canonical_ignores_how_a_disjunction_was_grouped():
    """`||` is associative, so where a writer put the parentheses says nothing
    about the machine -- and a run of same-target branches merges into one of
    these, so the grouping is this module's own choice on one side."""
    left_grouped = b"\xb9\xba\x96\x4f\xb8\x99\x41\x99\xa1"      # (A || B) || C
    right_grouped = b"\xb9\xba\x96\x4f\xb8\x41\x99\x99\xa1"     # A || (B || C)
    assert esdmerge.canonical(_state(0, [_cond(5, left_grouped)])) == \
        esdmerge.canonical(_state(0, [_cond(5, right_grouped)]))


def test_canonical_merges_same_target_siblings():
    """Two adjacent branches to the same state with nothing else attached are
    one branch; Melina writes them merged where vanilla writes them apart."""
    two = _state(0, [_cond(5, GUARD), _cond(5, OTHER)])
    one = _state(0, [_cond(5, GUARD_OR_OTHER)])
    assert esdmerge.canonical(two) == esdmerge.canonical(one)


def test_canonical_merges_a_duplicated_branch_into_a_self_disjunction():
    """Group 2147483592 state 7: vanilla has the identical condition twice and
    Melina writes `E || E` -- it does not simplify that back down to `E`, so
    neither does this."""
    twice = _state(0, [_cond(5, GUARD), _cond(5, GUARD)])
    merged = _state(0, [_cond(5, GUARD_OR_GUARD)])
    assert esdmerge.canonical(twice) == esdmerge.canonical(merged)
    assert esdmerge.canonical(twice) != esdmerge.canonical(_state(0, [_cond(5, GUARD)]))


def test_canonical_will_not_merge_siblings_that_jump_somewhere_else():
    assert esdmerge.canonical(_state(0, [_cond(5, GUARD), _cond(6, OTHER)])) != \
        esdmerge.canonical(_state(0, [_cond(5, GUARD_OR_OTHER)]))


def _calling(blob):
    """One state whose entry command takes a single argument."""
    return esd.State(0, entry=(
        esd.CommandCall(1, 2, args=(esd.CommandArg(blob),)),))


def test_canonical_reads_a_command_argument():
    """A command's arguments are as much of what it does as its id -- an ESD
    talk command is `ShowShopMessage(3)`, not `ShowShopMessage`. Dropping them
    here makes two different calls compare equal."""
    assert esdmerge.canonical(_calling(b"\x42\xa1")) != \
        esdmerge.canonical(_calling(b"\x43\xa1"))


def test_canonical_reads_an_argument_through_the_same_decoder():
    """The re-spellings the three encoders disagree about show up in argument
    bytecode too, so a raw byte comparison of arguments would report an edit
    wherever a mod tool rewrote a call it never touched."""
    assert esdmerge.canonical(_calling(ALWAYS)) == \
        esdmerge.canonical(_calling(ALWAYS_INT32))


def test_a_mod_whose_only_edit_is_a_command_argument_is_not_dropped():
    """canonical drives change detection, so an edit it cannot see is an edit
    that never happened: the group reads as untouched and base's copy ships."""
    def spelling(blob):
        return esd.Esd(groups=(esd.StateGroup(24, (_calling(blob),)),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    merged, notes = esdmerge.merge(spelling(b"\x42\xa1"), spelling(b"\x43\xa1"),
                                   spelling(b"\x42\xa1"))
    shipped, = _group(merged, 24).states[0].entry
    assert [a.bytecode for a in shipped.args] == [b"\x43\xa1"]
    assert notes == []


def test_canonical_keeps_a_real_difference():
    """The rules must not be so aggressive that a genuine edit disappears."""
    assert esdmerge.canonical(_state(0, [_cond(5)])) != \
        esdmerge.canonical(_state(0, [_cond(6)]))
    assert esdmerge.canonical(_state(0, [_cond(5, b"\x4f\xb8\x42\x95\xa1")])) != \
        esdmerge.canonical(_state(0, [_cond(5, b"\x4f\xb8\x43\x95\xa1")]))


def test_canonical_will_not_fold_same_target_branches_with_no_evaluator():
    """A condition with no evaluator bytes has no expression to disjoin, so the
    two-branches-are-one rule has nothing to fold and must leave them alone.

    Folding them built `None || None` and came out as a TypeError, which erm
    does not catch -- it only catches ErmError -- so a file with two of these
    side by side reached the user as a stack trace. And it is on the hot path:
    change detection decodes every group of all three files.
    """
    empty = lambda: esd.Condition(target=5, evaluator=b"")
    _sid, conditions, _entry, _exit, _while = esdmerge.canonical(
        _state(0, [empty(), empty()]))
    assert len(conditions) == 2
    assert [c[1] for c in conditions] == [None, None]


def test_a_group_with_an_empty_evaluator_merges_instead_of_crashing():
    spelling = lambda target: esd.Esd(
        groups=(esd.StateGroup(24, (
            esd.State(0, conditions=(esd.Condition(target=target, evaluator=b""),
                                     esd.Condition(target=target, evaluator=b""))),
            esd.State(1))),),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)
    merged, notes = esdmerge.merge(spelling(1), spelling(1), spelling(1))
    assert notes == []
    assert [s.id for s in _group(merged, 24).states] == [0, 1]


def test_canonical_refuses_bytecode_it_cannot_decode():
    """An opcode the decoder has never seen means the canonical form is a
    guess. Say so instead of quietly reporting two machines as different."""
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.canonical(_state(0, [_cond(5, b"\xff\xa1")]))
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.canonical(_state(0, [_cond(5, b"\x95\xa1")]))     # underflows
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.canonical(_state(0, [_cond(5, b"\xaf\xa1")]))     # unwritten slot


def test_align_matches_states_by_id_when_content_agrees():
    left = esd.StateGroup(24, (_state(0, [_cond(1)]), _state(1, [])))
    right = esd.StateGroup(24, (_state(0, [_cond(1)]), _state(1, [])))
    aligned = esdmerge.align(left, right)
    assert aligned.pairs == {0: 0, 1: 1}
    assert (aligned.left_only, aligned.right_only, aligned.unresolved) == ((), (), ())


def test_align_follows_content_when_ids_were_renumbered():
    """Boss Res deletes states from the shared group and renumbers the
    survivors down. Matching by id would pair unrelated states and corrupt
    every jump target."""
    left = esd.StateGroup(24, (_state(26, [_cond(15)]),
                               _state(28, [_cond(16, OTHER)]),
                               _state(29, [])))
    right = esd.StateGroup(24, (_state(26, [_cond(16, OTHER)]), _state(27, [])))
    aligned = esdmerge.align(left, right)
    assert aligned.pairs[28] == 26        # same content, renumbered
    assert aligned.pairs[29] == 27
    assert 26 not in aligned.pairs        # left's 26 was deleted, not renamed
    assert aligned.left_only == (26,)


def test_canonicaliser_finds_exactly_the_groups_the_mods_edited():
    """The control the whole canonicaliser is answerable to.

    Any group where both mods agree with each other is a group neither edited,
    so it must also match vanilla. Residual noise has to be zero, or the
    canonicaliser is inventing differences and the merge will invent conflicts
    on machines nobody touched. It ran at 46 before the rules landed.
    """
    v, b, m = (esd.read(real_esd(w)) for w in ("vanilla", "bossres", "melina"))
    by = lambda a: {g.id: g for g in a.groups}
    V, B, M = by(v), by(b), by(m)

    shared = set(V) & set(B) & set(M)
    shape = lambda g: tuple(esdmerge.canonical(s) for s in g.states)
    agree = [gid for gid in shared if shape(B[gid]) == shape(M[gid])]
    noise = [gid for gid in agree if shape(B[gid]) != shape(V[gid])]
    assert noise == [], f"canonicaliser reports edits in untouched groups: {noise}"
    assert len(shared) == 85
    assert len(agree) == 83

    changed_b = {gid for gid in set(V) & set(B) if shape(B[gid]) != shape(V[gid])}
    changed_m = {gid for gid in set(V) & set(M) if shape(M[gid]) != shape(V[gid])}
    assert changed_b == {2147483616, 2147483624}
    assert changed_m == {2147483624}
    assert set(B) - set(V) == set(range(2147483498, 2147483508)) | \
        set(range(2147483513, 2147483518)) | set(range(2147483522, 2147483528)) | \
        set(range(2147483530, 2147483557)) | set(range(2147483558, 2147483561))
    assert set(M) - set(V) == {2147482648}
    assert set(V) - set(B) == {2147483563}
    assert set(V) - set(M) == set()


def test_align_pairs_every_state_of_a_group_both_mods_left_alone():
    """Alignment on a real group: same machine, three encoders, and every state
    has to find its counterpart or the delta replay has nothing to replay onto."""
    v, b = (esd.read(real_esd(w)) for w in ("vanilla", "bossres"))
    untouched = 2147483573        # trivial-wrapper nesting, edited by nobody
    vg = next(g for g in v.groups if g.id == untouched)
    bg = next(g for g in b.groups if g.id == untouched)
    aligned = esdmerge.align(vg, bg)
    assert aligned.pairs == {s.id: s.id for s in vg.states}
    assert (aligned.left_only, aligned.right_only, aligned.unresolved) == ((), (), ())


def test_align_follows_a_renumbering_through_the_jump_targets():
    """The group both mods edited, which is the one a merge has to align.

    Boss Res deletes states from x24 and renumbers the survivors down, so a
    state's content stops matching the moment it jumps at one of them. Matching
    on content alone hands vanilla state 3 (`IF 1 -> 32`) to Boss Res state 16,
    an unrelated trampoline that still points at 32, while the real counterpart
    -- Boss Res state 3, retargeted to 30 -- goes unused. The targets have to be
    read through the mapping, which means resolving it is a fixed point.
    """
    v, b = (esd.read(real_esd(w)) for w in ("vanilla", "bossres"))
    group = lambda a: next(g for g in a.groups if g.id == 2147483624)
    vg, bg = group(v), group(b)
    aligned = esdmerge.align(vg, bg)

    assert aligned.pairs[3] == 3
    assert aligned.pairs[1] == 1
    # Every pair is either an identity or the small downward shift a dense
    # renumbering produces -- no state jumps forward past its neighbours.
    shifts = {left - right for left, right in aligned.pairs.items()}
    assert shifts <= {0, 1, 2, 3}, sorted(shifts)
    assert aligned.pairs[29] == 27 and aligned.pairs[42] == 39

    # Nothing may go missing quietly: every state is accounted for on both sides.
    assert set(aligned.pairs) | set(aligned.left_only) | set(aligned.unresolved) == \
        {s.id for s in vg.states}
    assert set(aligned.pairs.values()) | set(aligned.right_only) <= {s.id for s in bg.states}


def test_align_separates_a_deleted_state_from_one_it_could_not_place():
    """A caller replaying a delta has to tell "this state is gone" from "the
    alignment gave up", and a state missing from the pairs looks the same
    either way."""
    left = esd.StateGroup(24, (_state(0, [_cond(9, GUARD)]),     # nothing like it
                               _state(1, [_cond(2, OTHER)]),     # two identical
                               _state(2, [_cond(2, OTHER)])))    # candidates
    right = esd.StateGroup(24, (_state(5, [_cond(2, OTHER)]),
                                _state(6, [_cond(2, OTHER)])))
    aligned = esdmerge.align(left, right)
    assert aligned.left_only == (0,)
    assert aligned.unresolved == (1, 2)
    assert aligned.pairs == {}


def test_align_resolves_look_alike_states_by_iterating_on_the_targets():
    """Renumbered on both sides, with nothing but the jump targets to tell
    three look-alike states apart.

    One pass cannot do it. States 2, 3 and 4 are identical until their targets
    are known, and each one's target is only known once the next is paired, so
    the answer arrives one state per round working back from the anchor. The id
    ranges are disjoint, so a left id read as a right id resolves to nothing.
    """
    left = esd.StateGroup(24, (_state(1, [_cond(2, GUARD)]),
                               _state(2, [_cond(3, OTHER)]),
                               _state(3, [_cond(4, OTHER)]),
                               _state(4, [_cond(5, OTHER)]),
                               _state(5, [])))
    right = esd.StateGroup(24, (_state(11, [_cond(12, GUARD)]),
                                _state(12, [_cond(13, OTHER)]),
                                _state(13, [_cond(14, OTHER)]),
                                _state(14, [_cond(15, OTHER)]),
                                _state(15, [])))
    aligned = esdmerge.align(left, right)
    assert aligned.pairs == {1: 11, 2: 12, 3: 13, 4: 14, 5: 15}
    assert (aligned.left_only, aligned.right_only, aligned.unresolved) == ((), (), ())


# --- change detection -------------------------------------------------------


def test_a_group_three_encoders_spell_differently_is_not_a_conflict():
    """The reason change detection cannot compare raw structure. Nobody edited
    this group; each of the three files just writes "always" its own way. Read
    literally that is two conflicting edits, and the merge would refuse a
    machine none of the three disagrees about."""
    spelling = lambda evaluator: esd.Esd(
        groups=(esd.StateGroup(24, (
            esd.State(0, conditions=(esd.Condition(target=1, evaluator=evaluator),)),
            esd.State(1))),),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    merged, notes = esdmerge.merge(spelling(ALWAYS_INT32), spelling(ALWAYS_EQ_ONE),
                                   spelling(ALWAYS))
    assert notes == []
    assert _find(_group(merged, 24), 0).conditions[0].evaluator == ALWAYS_INT32


# --- delta replay -----------------------------------------------------------


def test_shared_group_replays_an_added_state_and_its_hook():
    """Melina's shape: she adds a state and redirects an existing transition into
    it. The redirect has to land on the base's copy of that state."""
    vanilla = _esd({24: [(15, None), (26, 15)]})
    base = _esd({24: [(15, None), (26, 15), (99, 15)]})       # base added its own
    other = _esd({24: [(15, None), (26, 44)]})
    other = other._replace(groups=(
        esd.StateGroup(24, other.groups[0].states + (
            esd.State(44, conditions=(esd.Condition(target=15, evaluator=GUARD),)),)),))

    merged, notes = esdmerge.merge(base, other, vanilla)
    group = _group(merged, 24)
    ids = [s.id for s in group.states]
    assert 44 in ids and 99 in ids          # both sides' additions survive
    assert _targets(_find(group, 26)) == [44]   # the redirect was replayed
    assert _targets(_find(group, 44)) == [15]   # and the graft still lands
    assert notes == []


def test_shared_group_notes_a_state_the_base_deleted():
    """Where the other side edits a state the base removed, there is nowhere to
    replay it. Keep the base and say so -- silently dropping it is the failure this
    whole design is trying to avoid."""
    vanilla = _esd({24: [(15, None), (26, 15), (27, 15)]})
    base = _esd({24: [(15, None), (26, 15)]})                 # deleted 27
    other = _esd({24: [(15, None), (26, 15), (27, 999)]})     # edited 27
    other = other._replace(groups=(
        esd.StateGroup(24, other.groups[0].states + (esd.State(999),)),))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert any(n.group == 24 and n.state == 27 and n.reason is esdmerge.Reason.NOT_IN_BASE
               for n in notes), notes
    assert [s.id for s in _group(merged, 24).states] == [15, 26, 999]


def test_a_state_neither_mod_kept_is_not_reported_as_a_kept_copy():
    """"the preferred mod's copy was kept" is a claim about what shipped, and
    when both mods dropped the state there is no copy to have kept. Someone
    reading that note goes looking in the merged file for a state that is not
    there, and concludes the report is lying about something else too.
    """
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    hook = lambda target: esd.State(
        0, conditions=(esd.Condition(target=target, evaluator=ALWAYS),))
    distinctive = esd.State(2, conditions=(
        esd.Condition(target=1, evaluator=GUARD_OR_GUARD),))

    vanilla = group(hook(1), esd.State(1), distinctive)
    # Both mods drop vanilla 2, and each changes something else, so the group
    # still has to be merged state by state.
    base = group(hook(1), esd.State(1),
                 esd.State(8, conditions=(esd.Condition(target=1, evaluator=OTHER),)))
    other = group(hook(3), esd.State(1),
                  esd.State(3, conditions=(esd.Condition(target=1, evaluator=GUARD),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert 2 not in {s.id for s in _group(merged, 24).states}
    assert [(n.state, n.reason) for n in notes] == \
        [(2, esdmerge.Reason.NOT_IN_EITHER)]


def test_a_state_only_the_other_mod_dropped_still_reports_the_kept_copy():
    """The other half of the same branch: base's copy really is what shipped,
    and saying so is the whole value of the note."""
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    hook = lambda target: esd.State(
        0, conditions=(esd.Condition(target=target, evaluator=ALWAYS),))
    distinctive = esd.State(2, conditions=(
        esd.Condition(target=1, evaluator=GUARD_OR_GUARD),))

    vanilla = group(hook(1), esd.State(1), distinctive)
    base = group(hook(1), esd.State(1), distinctive,
                 esd.State(8, conditions=(esd.Condition(target=1, evaluator=OTHER),)))
    other = group(hook(3), esd.State(1),
                  esd.State(3, conditions=(esd.Condition(target=1, evaluator=GUARD),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert 2 in {s.id for s in _group(merged, 24).states}
    assert [(n.state, n.reason) for n in notes] == \
        [(2, esdmerge.Reason.NOT_IN_OTHER)]


def test_a_grafted_state_gets_an_id_that_is_free_in_the_base():
    """Both sides added a state under the same id. Renumbering the graft is the
    only option, and every reference to it has to move with it."""
    vanilla = _esd({24: [(0, 9), (9, None)]})
    base = _esd({24: [(0, 9), (9, None), (1, 9)]})            # base's own state 1
    other = _esd({24: [(0, 1), (9, None)]})                   # hooks its own state 1
    other = other._replace(groups=(
        esd.StateGroup(24, other.groups[0].states + (
            esd.State(1, conditions=(esd.Condition(target=9, evaluator=GUARD),)),)),))

    merged, _ = esdmerge.merge(base, other, vanilla)
    group = _group(merged, 24)
    assert len({s.id for s in group.states}) == len(group.states)   # no duplicate ids
    grafted, = _targets(_find(group, 0))
    assert grafted != 1                        # base already owns 1
    assert _targets(_find(group, grafted)) == [9]
    assert _targets(_find(group, 1)) == [9]    # base's own state 1 is untouched


def test_both_mods_changing_one_state_keeps_the_base_and_names_it():
    vanilla = _esd({24: [(0, 1), (1, None), (2, None)]})
    base = _esd({24: [(0, 2), (1, None), (2, None)]})         # redirected to 2
    other = _esd({24: [(0, 3), (1, None), (2, None)]})        # redirected to its own 3
    other = other._replace(groups=(
        esd.StateGroup(24, other.groups[0].states + (esd.State(3),)),))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert _targets(_find(_group(merged, 24), 0)) == [2]      # base wins
    assert any(n.group == 24 and n.state == 0 and n.reason is esdmerge.Reason.BOTH_CHANGED
               for n in notes), notes


def test_an_edit_alignment_cannot_place_is_named_rather_than_dropped():
    """align pairs states on content, so a state whose *commands* changed reads
    as a delete plus an add rather than as an edit -- there is nothing left that
    looks like the vanilla state to replay onto. The edit must not vanish."""
    def one(entry_id, extra=()):
        return esd.Esd(
            groups=(esd.StateGroup(24, (
                esd.State(0, conditions=(esd.Condition(target=1, evaluator=ALWAYS),),
                          entry=(esd.CommandCall(1, entry_id),)),
                esd.State(1)) + extra),),
            name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    vanilla = one(100)
    base = one(100, extra=(esd.State(2, conditions=(
        esd.Condition(target=1, evaluator=GUARD),)),))        # base changed elsewhere
    other = one(200)                                          # other changed the call

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert any(n.group == 24 and n.state == 0 and n.reason is esdmerge.Reason.NOT_IN_OTHER
               for n in notes), notes
    # The other half of the same limitation: the rewritten state is also
    # grafted, and nothing reaches it. A dead machine in the output is only
    # tolerable because it is named -- so the naming is what gets pinned.
    orphan, = [n for n in notes if n.reason is esdmerge.Reason.ORPHANED_GRAFT]
    assert orphan.target not in _reachable(_group(merged, 24))


def test_a_replayed_state_carries_no_leftover_order():
    """A replayed state's records come from the other file's tables, and table
    indices only mean anything inside the file they were read from.

    Every record other's state 0 carries -- the condition, its pass command and
    that command's argument -- is stamped with the index base already uses for a
    *different* record of the same kind (its state 2's). Left uncleared, each one
    is indistinguishable from a genuine shared row and write() refuses the lot.
    """
    def replayable(target, orders, call_id, blob):
        """One state whose only difference from vanilla's is where it jumps."""
        index, blob_index = orders
        return esd.State(0, order=0, conditions=(esd.Condition(
            target=target, evaluator=ALWAYS, order=index, blob_order=blob_index,
            pass_commands=(esd.CommandCall(1, call_id, order=index, args=(
                esd.CommandArg(blob, order=index, blob_order=blob_index),)),)),))

    vanilla = esd.Esd(groups=(esd.StateGroup(24, (
        replayable(1, (None, None), 7, b"\x41\xa1"), esd.State(1)),),),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)
    base = esd.Esd(groups=(esd.StateGroup(24, (
        replayable(1, (0, 0), 7, b"\x41\xa1"),
        esd.State(1, order=1),
        # Base's own addition, and the owner of every table index other's state
        # 0 also claims.
        esd.State(2, order=2, conditions=(esd.Condition(
            target=1, evaluator=GUARD, order=1, blob_order=1,
            pass_commands=(esd.CommandCall(1, 8, order=1, args=(
                esd.CommandArg(b"\x42\xa1", order=1, blob_order=2),)),)),)),
    ), order=0),), name="t000001000", unk=(0, 0, 0, 0), pool_count=0)
    other = esd.Esd(groups=(esd.StateGroup(24, (
        replayable(3, (1, 1), 7, b"\x41\xa1"),      # redirected to a state she adds
        esd.State(1, order=1),
        esd.State(3, order=2),
    ), order=0),), name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert notes == []
    replayed = _find(_group(merged, 24), 0)
    assert _targets(replayed) == [3]
    for condition in replayed.conditions:
        _assert_condition_degrafted(condition)
    assert {s.id for s in esd.read(esd.write(merged)).groups[0].states} == {0, 1, 2, 3}


# --- structural invariants --------------------------------------------------


def test_check_accepts_the_three_real_files():
    """The invariants have to be ones a shipped file already satisfies, or they
    are not invariants -- they are just this module's opinion."""
    for which in ("vanilla", "bossres", "melina"):
        esdmerge.check(esd.read(real_esd(which)))


def test_check_rejects_a_dangling_jump_target():
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.check(_esd({1: [(0, 99)]}))


def test_check_rejects_a_dangling_target_inside_a_subcondition():
    """A graft's targets get rewritten recursively, so the check has to look
    just as deep -- a nested branch is where a missed rewrite would hide."""
    archive = _esd({1: [(0, None)]})
    nested = esd.State(0, conditions=(esd.Condition(
        target=None, evaluator=GUARD,
        subconditions=(esd.Condition(target=99, evaluator=ALWAYS),)),))
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.check(archive._replace(
            groups=(archive.groups[0]._replace(states=(nested,)),)))


def test_check_rejects_duplicate_state_ids():
    archive = _esd({1: [(0, None)]})
    twice = archive.groups[0].states * 2
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.check(archive._replace(
            groups=(archive.groups[0]._replace(states=twice),)))


def test_check_rejects_a_call_to_a_missing_machine():
    archive = _esd({1: [(0, None)]})
    caller = archive.groups[0].states[0]._replace(
        entry=(esd.CommandCall(bank=esd.CALL_BANK, id=2147482648),))
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.check(archive._replace(
            groups=(archive.groups[0]._replace(states=(caller,)),)))


def test_check_ignores_other_banks():
    """Only bank 6 command ids name a state group. Banks 1/5/7 are ordinary
    engine calls whose ids collide with group ids by coincidence."""
    archive = _esd({1: [(0, None)]})
    caller = archive.groups[0].states[0]._replace(
        entry=(esd.CommandCall(bank=1, id=2147482648),))
    esdmerge.check(archive._replace(
        groups=(archive.groups[0]._replace(states=(caller,)),)))


def test_check_rejects_a_call_from_a_pass_command():
    archive = _esd({1: [(0, None)]})
    caller = archive.groups[0].states[0]._replace(conditions=(esd.Condition(
        target=None, evaluator=ALWAYS,
        pass_commands=(esd.CommandCall(bank=esd.CALL_BANK, id=77),)),))
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.check(archive._replace(
            groups=(archive.groups[0]._replace(states=(caller,)),)))


# --- the real pair ----------------------------------------------------------


def test_the_real_shared_group_replays_melina_onto_boss_res():
    """2147483624 is the one group both mods change, and it is the whole reason
    this merge exists. Boss Res deletes vanilla 26, 27 and 41 and renumbers the
    survivors down; Melina hooks vanilla 26 and 29 into two states she adds.

    One of her two hooks lands on a state Boss Res deleted. That one cannot be
    replayed and is named. The other survives, and with it the path into her
    machine.
    """
    v, b, m = (esd.read(real_esd(w)) for w in ("vanilla", "bossres", "melina"))
    merged, notes = esdmerge.merge(b, m, v)
    group = _group(merged, 2147483624)

    # Boss Res's 40 survivors, plus Melina's two additions at their own ids.
    assert sorted(s.id for s in group.states) == list(range(40)) + [43, 44]

    # Vanilla 29 is Boss Res's 27. Her redirect had to move with the renumbering.
    assert _targets(_find(group, 27)) == [43]
    assert _targets(_find(group, 43)) == [44]
    assert _targets(_find(group, 44)) == [15]
    assert [(c.bank, c.id) for c in _find(group, 44).entry] == [(6, 2147482648)]

    # The machine she calls came across with her, so the call resolves.
    assert 2147482648 in {g.id for g in merged.groups}

    # Her machine is still entered: nothing above is worth anything if the
    # surviving hook sits on an orphan.
    assert {27, 43, 44} <= _reachable(group)

    # Her other hook was on vanilla 26, which Boss Res deleted. That is the only
    # thing wrong with this merge: in particular neither state she added arrives
    # orphaned, so the graft-reachability check has nothing to say.
    assert len(notes) == 1, notes
    assert notes[0].group == 2147483624
    assert notes[0].state == 26
    assert notes[0].reason is esdmerge.Reason.NOT_IN_BASE


def test_the_real_merge_writes_and_survives_a_round_trip():
    v, b, m = (esd.read(real_esd(w)) for w in ("vanilla", "bossres", "melina"))
    merged, _ = esdmerge.merge(b, m, v)
    reread = esd.read(esd.write(merged))
    esdmerge.check(reread)

    # Boss Res's own groups, plus the machine Melina brought.
    assert {g.id for g in reread.groups} == \
        {g.id for g in b.groups} | {2147482648}
    assert sorted(s.id for s in _group(reread, 2147483624).states) == \
        list(range(40)) + [43, 44]


def test_a_graft_that_jumps_at_a_deleted_state_is_refused():
    """A new state whose jump has nowhere to land cannot be grafted quietly:
    silently blanking the target turns a branch into a fallthrough, which is a
    different machine that still loads."""
    vanilla = _esd({24: [(0, 1), (1, None), (2, None)]})
    base = _esd({24: [(0, 1), (1, None)]})                    # deleted 2
    other = _esd({24: [(0, 1), (1, None), (2, None)]})
    other = other._replace(groups=(
        esd.StateGroup(24, other.groups[0].states + (
            esd.State(3, conditions=(esd.Condition(target=2, evaluator=GUARD),)),)),))

    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.merge(base, other, vanilla)


def test_a_replay_that_jumps_at_a_deleted_state_keeps_the_base_and_names_it():
    vanilla = _esd({24: [(0, 1), (1, None), (2, None)]})
    base = _esd({24: [(0, 1), (1, None)]})                    # deleted 2
    other = _esd({24: [(0, 2), (1, None), (2, None)]})        # redirected 0 at it

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert _targets(_find(_group(merged, 24), 0)) == [1]      # base's, untouched
    assert any(n.group == 24 and n.state == 0 and n.reason is esdmerge.Reason.UNPLACEABLE_JUMP
               and n.target == 2 for n in notes), notes


def test_states_alignment_could_not_tell_apart_are_named_not_duplicated():
    """`unresolved` is not `right_only`. Treating a state the alignment gave up
    on as an addition grafts a second copy of a machine that is already there."""
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    look_alike = lambda sid: esd.State(
        sid, conditions=(esd.Condition(target=7, evaluator=OTHER),))
    anchor = esd.State(0, conditions=(esd.Condition(target=7, evaluator=GUARD),))

    vanilla = group(anchor, look_alike(1), look_alike(2), esd.State(7))
    base = group(anchor, look_alike(1), look_alike(2), esd.State(7),
                 esd.State(8, conditions=(esd.Condition(target=7, evaluator=GUARD),)))
    other = group(anchor, look_alike(5), look_alike(6), esd.State(7))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert [s.id for s in _group(merged, 24).states] == [0, 1, 2, 7, 8]
    assert len(notes) == 2
    assert all(n.group == 24 and n.reason is esdmerge.Reason.UNRESOLVED_IN_OTHER
              for n in notes), notes
    assert {n.state for n in notes} == {1, 2}


def test_merge_refuses_to_return_a_graph_that_does_not_hold_together():
    """The check is only worth having if it runs on the way out."""
    vanilla = _esd({1: [(0, None)]})
    other = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)]})
    dangling = base.groups[0].states[0]._replace(
        conditions=(esd.Condition(target=99, evaluator=ALWAYS),))
    base = base._replace(groups=(base.groups[0]._replace(states=(dangling,)),))

    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.merge(base, other, vanilla)


def test_a_renumbered_but_unedited_state_is_not_read_as_a_second_edit():
    """Telling "the preferred mod also changed this state" from "the preferred
    mod renumbered around it" is what decides whether the other mod's edit gets
    replayed or refused.

    Base deletes vanilla 2 and its state 3 becomes 2, so base's copy of state 0
    now jumps at `2` where vanilla's jumps at `3`. Compared literally that is an
    edit, and the merge would call a conflict on a state base never touched.
    """
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    hook = lambda target: esd.State(
        0, conditions=(esd.Condition(target=target, evaluator=ALWAYS),))
    keep = lambda sid, target: esd.State(
        sid, conditions=(esd.Condition(target=target, evaluator=GUARD),))

    vanilla = group(hook(3), keep(1, 3),
                    esd.State(2, conditions=(
                        esd.Condition(target=3, evaluator=GUARD_OR_OTHER),)),
                    esd.State(3))
    base = group(hook(2), keep(1, 2), esd.State(2))       # dropped vanilla 2
    other = group(hook(4), keep(1, 3),                    # redirected 0 at a new state
                  esd.State(2, conditions=(
                      esd.Condition(target=3, evaluator=GUARD_OR_OTHER),)),
                  esd.State(3),
                  esd.State(4, conditions=(esd.Condition(target=3, evaluator=OTHER),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    group24 = _group(merged, 24)
    assert notes == []
    assert _targets(_find(group24, 0)) == [4]     # the redirect replayed
    assert _targets(_find(group24, 4)) == [2]     # onto base's numbering


def test_an_edit_with_no_single_home_in_the_base_is_not_reported_as_a_deletion():
    """Falling through to the deletion path keeps the right state -- base's --
    but says the wrong thing about why. "I deleted it" sends someone looking at
    the preferred mod's diff; "I could not tell which of these it is" is the
    alignment admitting it gave up, which is a different thing to go fix.
    """
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    look_alike = lambda sid, target=7: esd.State(
        sid, conditions=(esd.Condition(target=target, evaluator=OTHER),))
    anchor = esd.State(0, conditions=(esd.Condition(target=7, evaluator=GUARD),))

    vanilla = group(anchor, look_alike(1), look_alike(2), esd.State(7))
    base = group(anchor, look_alike(5), look_alike(6), esd.State(7))   # renumbered
    other = group(anchor, look_alike(1, target=8), look_alike(2),
                  esd.State(7), esd.State(8))                          # edited 1

    _merged, notes = esdmerge.merge(base, other, vanilla)
    assert len(notes) == 1, notes
    assert notes[0].group == 24
    assert notes[0].state == 1
    assert notes[0].reason is esdmerge.Reason.UNRESOLVED_IN_BASE


# --- grafts that arrive dead ------------------------------------------------


def test_a_graft_orphaned_by_a_refused_replay_is_named():
    """The hook that would have entered the other mod's new state lost a
    conflict, so the state is grafted with nothing left pointing at it.

    Structurally the output is perfect -- no duplicate ids, no dangling target,
    every call resolves -- and the machine is dead. The note about the lost hook
    does not say that a whole machine came across as unreachable code, and that
    is the difference between "your redirect was overruled" and "this mod does
    nothing now".
    """
    vanilla = _esd({24: [(0, 1), (1, None), (9, None)]})
    base = _esd({24: [(0, 9), (1, None), (9, None)]})       # redirected 0 at 9
    other = _esd({24: [(0, 3), (1, None), (9, None), (3, 1)]})   # 0 through a new 3

    merged, notes = esdmerge.merge(base, other, vanilla)
    group = _group(merged, 24)
    assert 3 in {s.id for s in group.states}      # grafted, as before
    assert 3 not in _reachable(group)             # and nothing reaches it
    assert any(n.group == 24 and n.state == 3 and n.reason is esdmerge.Reason.ORPHANED_GRAFT
               and n.target == 3 for n in notes), notes


def test_a_graft_the_other_mod_could_not_reach_either_is_not_named():
    """The invariant is relative, not absolute. Shipped files are full of states
    nothing jumps at -- 22 of vanilla's, across 10 of its 86 groups -- so a
    graft that was already dead in the mod it came from is not news."""
    vanilla = _esd({24: [(0, 1), (1, None)]})
    base = _esd({24: [(0, 1), (1, None), (8, 1)]})          # base's own addition
    other = _esd({24: [(0, 1), (1, None), (3, 1)]})         # nothing points at 3

    merged, notes = esdmerge.merge(base, other, vanilla)
    group = _group(merged, 24)
    assert 3 in {s.id for s in group.states} and 3 not in _reachable(group)
    assert notes == []


def test_reachability_starts_where_the_writer_says_the_group_starts():
    """A group's entry is its lowest-`order` state row -- what esd.write points
    the group header at -- and not whatever sits first in the states tuple. The
    two agree for anything read off disk, so nothing in the real files tells
    them apart, and a walk that picks the wrong one reports a live machine as
    dead or the reverse.

    Here the other mod's states are in an order the tuple does not reflect. Its
    hook into the state it adds hangs off the real entry row, so starting from
    the tuple's first state finds nothing, the graft reads as dead-on-arrival,
    and the note saying the mod now does nothing never gets written.
    """
    def group(*states):
        return esd.Esd(groups=(esd.StateGroup(24, states, order=0),),
                       name="t000001000", unk=(0, 0, 0, 0), pool_count=0)

    hook = lambda target, order: esd.State(
        0, order=order, conditions=(esd.Condition(target=target, evaluator=ALWAYS),))

    vanilla = group(hook(1, 0), esd.State(1, order=1), esd.State(9, order=2))
    base = group(hook(9, 0), esd.State(1, order=1), esd.State(9, order=2))
    other = group(esd.State(1, order=1),          # tuple order is not table order
                  hook(3, 0),
                  esd.State(9, order=2),
                  esd.State(3, order=3,
                            conditions=(esd.Condition(target=1, evaluator=GUARD),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert 3 in {s.id for s in _group(merged, 24).states}
    assert any(n.group == 24 and n.state == 3
               and n.reason is esdmerge.Reason.ORPHANED_GRAFT for n in notes), notes


def _nested_hook(target):
    """A state whose only branch is one level down -- `IF guard: { IF x -> t }`,
    the shape vanilla writes where both mods write it flat."""
    return esd.State(0, conditions=(esd.Condition(
        target=None, evaluator=GUARD,
        subconditions=(esd.Condition(target=target, evaluator=OTHER),)),))


def _one_group(*states):
    return esd.Esd(groups=(esd.StateGroup(24, states),),
                   name="t000001000", unk=(0, 0, 0, 0), pool_count=0)


def test_a_hook_written_as_a_nested_branch_is_retargeted_too():
    """Both mods added a state 3, so the graft has to be renumbered -- and the
    reference that has to move with it is buried in a subcondition. Left alone
    it still resolves, to the preferred mod's unrelated state 3, which is the
    quiet kind of wrong this whole alignment exists to avoid."""
    vanilla = _one_group(_nested_hook(1), esd.State(1))
    base = _one_group(_nested_hook(1), esd.State(1),
                      esd.State(3, conditions=(esd.Condition(target=1, evaluator=GUARD),)))
    other = _one_group(_nested_hook(3), esd.State(1),
                       esd.State(3, conditions=(esd.Condition(target=1, evaluator=OTHER),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    group = _group(merged, 24)
    grafted, = _targets(_find(group, 0))
    assert grafted != 3                        # base already owns 3
    assert grafted in _reachable(group)
    assert _targets(_find(group, 3)) == [1]    # base's own state 3, untouched
    assert notes == []


def test_a_graft_hooked_by_a_nested_branch_is_still_seen_as_live():
    """The relative invariant compares two reachability walks, so a walk that
    skips nested branches breaks both sides equally and the comparison still
    balances -- it just balances at "this graft was dead anyway", and the orphan
    goes unreported. Here the graft's only in-edge in the other mod is nested."""
    vanilla = _one_group(_nested_hook(1), esd.State(1), esd.State(9))
    base = _one_group(_nested_hook(9), esd.State(1), esd.State(9))     # took the hook
    other = _one_group(_nested_hook(3), esd.State(1), esd.State(9),
                       esd.State(3, conditions=(esd.Condition(target=1, evaluator=OTHER),)))

    merged, notes = esdmerge.merge(base, other, vanilla)
    assert 3 not in _reachable(_group(merged, 24))
    assert any(n.group == 24 and n.state == 3 and n.reason is esdmerge.Reason.ORPHANED_GRAFT
               and n.target == 3 for n in notes), notes


# --- rendering a note -------------------------------------------------------


# What each sentence has to actually say, as (phrases it must contain, phrases
# it must not). The notes are the whole product of merge-and-flag: they are what
# turns "go check the mod still works" into "go check state 26 of group
# 2147483624", so a reason rendering to the wrong sentence sends someone looking
# at the wrong mod's diff. Not the full prose -- rewording stays free -- just the
# words that make one reason a different statement from its neighbours.
_MUST_SAY = {
    esdmerge.Reason.UNRESOLVED_IN_OTHER:
        (("several", "other mod's states", "not applied"), ("preferred",)),
    esdmerge.Reason.NOT_IN_OTHER:
        (("the other mod", "no state matching", "copy was kept"), ("several",)),
    esdmerge.Reason.NOT_IN_EITHER:
        (("neither mod", "state matching"), ("several", "was kept")),
    esdmerge.Reason.UNRESOLVED_IN_BASE:
        (("several", "preferred mod's states", "not applied"), ()),
    esdmerge.Reason.NOT_IN_BASE:
        (("preferred mod deleted", "not applied"), ("several",)),
    esdmerge.Reason.BOTH_CHANGED:
        (("both mods changed", "kept the preferred mod's"), ("not applied",)),
    esdmerge.Reason.UNPLACEABLE_JUMP:
        (("jumps to", "no counterpart", "not applied"), ("several",)),
    esdmerge.Reason.ORPHANED_GRAFT:
        (("nothing", "reaches it", "will not run"), ("not applied",)),
}


def test_every_reason_says_what_it_means():
    """A missing prose entry would raise KeyError the first time an apply hit
    it, and a wrong one is worse than that: it is confidently misleading."""
    assert set(_MUST_SAY) == set(esdmerge.Reason)
    for reason, (must, must_not) in _MUST_SAY.items():
        text = esdmerge.describe(esdmerge.Note(
            group=2147483624, state=26, reason=reason, target=44))
        assert "2147483624" in text and "26" in text, (reason, text)
        for phrase in must:
            assert phrase in text, (reason, phrase, text)
        for phrase in must_not:
            assert phrase not in text, (reason, phrase, text)


def test_no_two_reasons_render_to_the_same_sentence():
    rendered = [esdmerge.describe(esdmerge.Note(group=24, state=1, reason=r, target=2))
                for r in esdmerge.Reason]
    assert len(set(rendered)) == len(rendered)


def test_describe_names_the_state_a_graft_landed_at():
    """ORPHANED_GRAFT is the one reason whose `state` is the other mod's own id
    rather than a vanilla one, and both numbers matter: one identifies the mod's
    state, the other says where to look for it in the merged file."""
    text = esdmerge.describe(esdmerge.Note(
        group=2147483624, state=43, reason=esdmerge.Reason.ORPHANED_GRAFT, target=41))
    assert "43" in text and "41" in text
