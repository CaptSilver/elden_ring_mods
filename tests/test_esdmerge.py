import pytest

from ermlib import esdmerge
from ermlib.formats import esd


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
