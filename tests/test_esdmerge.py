import pytest

from ermlib import esdmerge
from ermlib.formats import esd
from tests.esd_fixtures import real_esd


def _esd(groups):
    """groups: {group_id: [(state_id, target_or_None)]}"""
    return esd.Esd(
        groups=tuple(
            esd.StateGroup(gid, tuple(
                esd.State(sid, conditions=() if target is None else
                          (esd.Condition(target=target, evaluator=b"\xa1"),))
                for sid, target in states))
            for gid, states in sorted(groups.items())),
        name="t000001000", unk=(0, 0, 0, 0), pool_count=0)


def _ids(archive):
    return sorted(g.id for g in archive.groups)


def test_a_group_only_the_other_side_added_is_grafted():
    vanilla = _esd({1: [(0, None)]})
    base = _esd({1: [(0, None)]})
    other = _esd({1: [(0, None)], 1000: [(0, None), (1, 0)]})
    merged, notes = esdmerge.merge(base, other, vanilla)
    assert _ids(merged) == [1, 1000]
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


def test_a_group_both_sides_modified_is_refused_for_now():
    vanilla = _esd({24: [(0, None)]})
    base = _esd({24: [(0, None), (1, 0)]})
    other = _esd({24: [(0, None), (2, 0)]})
    with pytest.raises(esdmerge.EsdMergeError):
        esdmerge.merge(base, other, vanilla)


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
                    esd.Condition(target=1, evaluator=b"\xa1", order=0,
                                  blob_order=0),
                )),
                esd.State(1, order=2, entry=(
                    esd.CommandCall(6, 5, args=(
                        esd.CommandArg(b"\x40", order=0, blob_order=100),
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
            esd.Condition(target=None, evaluator=b"\xb0", order=0, blob_order=0),
        ), entry=(
            esd.CommandCall(9, 9, args=(
                esd.CommandArg(b"\xa0", order=0, blob_order=0),
            ), order=0),
        )),
    ), order=0)
    vanilla = esd.Esd(groups=(group1,), name="t000001000",
                      unk=(0, 0, 0, 0), pool_count=0)
    base = vanilla       # group 1 is untouched by other -> base's stamped
                         # order/blob_order values above are what land in
                         # the merged output, unmodified

    graft_arg = esd.CommandArg(b"\x99", order=0, blob_order=0)      # collides with base's arg
    graft_call = esd.CommandCall(1, 1, args=(graft_arg,), order=0)  # collides with base's call
    graft_sub = esd.Condition(target=None, evaluator=b"\xd0",
                              order=0, blob_order=0)                # collides with base's cond
    graft_cond = esd.Condition(target=1, evaluator=b"\xc0",
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
#
# Real bytecode, in the spellings the three files actually use. A condition
# evaluator is a postfix stack program: 0x40+n pushes a small int, 0x82 pushes
# an int32, 0x80/0x81 push a float32/float64, 0xB8/0xB9/0xBA are builtins,
# 0x95/0x96/0x98/0x99 are == != && ||, and 0xA1 terminates it.

ALWAYS = b"\x41\xa1"                          # push 1 -- FromSoft's "IF 1"
ALWAYS_INT32 = b"\x82\x01\x00\x00\x00\xa1"    # both mods' spelling of the same
GUARD = b"\xb9\xba\x96\xa1"                   # b9 != ba
OTHER = b"\x4f\xb8\xa1"                       # b8(15)
GUARD_AND_OTHER = b"\xb9\xba\x96\x4f\xb8\x98\xa1"
GUARD_OR_OTHER = b"\xb9\xba\x96\x4f\xb8\x99\xa1"
GUARD_OR_GUARD = b"\xb9\xba\x96\xb9\xba\x96\x99\xa1"


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


def test_canonical_keeps_a_real_difference():
    """The rules must not be so aggressive that a genuine edit disappears."""
    assert esdmerge.canonical(_state(0, [_cond(5)])) != \
        esdmerge.canonical(_state(0, [_cond(6)]))
    assert esdmerge.canonical(_state(0, [_cond(5, b"\x4f\xb8\x42\x95\xa1")])) != \
        esdmerge.canonical(_state(0, [_cond(5, b"\x4f\xb8\x43\x95\xa1")]))


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
    assert esdmerge.align(left, right) == {0: 0, 1: 1}


def test_align_follows_content_when_ids_were_renumbered():
    """Boss Res deletes states from the shared group and renumbers the
    survivors down. Matching by id would pair unrelated states and corrupt
    every jump target."""
    left = esd.StateGroup(24, (_state(26, [_cond(15)]),
                               _state(28, [_cond(16, OTHER)]),
                               _state(29, [])))
    right = esd.StateGroup(24, (_state(26, [_cond(16, OTHER)]), _state(27, [])))
    mapping = esdmerge.align(left, right)
    assert mapping[28] == 26        # same content, renumbered
    assert mapping[29] == 27
    assert 26 not in mapping        # left's 26 was deleted, not renamed


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
    has to find its counterpart or Task 5 has nothing to replay a delta onto."""
    v, b = (esd.read(real_esd(w)) for w in ("vanilla", "bossres"))
    untouched = 2147483573        # trivial-wrapper nesting, edited by nobody
    vg = next(g for g in v.groups if g.id == untouched)
    bg = next(g for g in b.groups if g.id == untouched)
    mapping = esdmerge.align(vg, bg)
    assert mapping == {s.id: s.id for s in vg.states}
