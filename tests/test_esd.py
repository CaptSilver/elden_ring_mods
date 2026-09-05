import struct

import pytest

from ermlib.formats import esd
from tests.esd_fixtures import EXPECTED, real_esd


def _walk_conditions(conditions):
    """Depth-first through a condition list and its nested subconditions."""
    for c in conditions:
        yield c
        yield from _walk_conditions(c.subconditions)


def _condition_count(raw):
    """The condition-table row count an ESD header declares -- descriptor slot 3
    of the six (size, count) pairs at INTERNAL_HEADER+0x1C, the same field
    esd.read() checks the table layout against."""
    return struct.unpack_from("<I", raw, 0x38)[0]


def _declared_pool_count(raw):
    """The condition-pointer-pool slot count an ESD header declares, at
    INTERNAL_HEADER+0x50 -- the same field esd.read() reads. Vanilla declares
    more slots here than its graph reaches."""
    return struct.unpack_from("<I", raw, 0x50)[0]


def _synthetic(groups, name="t000001000", extra_pool_slots=0):
    """Build a minimal valid ESD. groups: [(group_id, [(state_id, target_or_None)])].

    extra_pool_slots pads the condition-pointer pool with orphaned slots no
    state reaches, the way vanilla's compiler does.

    Hand-rolled rather than going through esd.write, so the reader is tested
    against bytes this file assembled. Not an independent implementation of the
    format, though: the trailing dummy state row, the pool layout and the
    zero-length -1 offsets are all write()'s conventions, copied here because
    they are what the reader has to accept. Only the real files in the fixtures
    are outside evidence about the format.
    """
    states, conditions, cond_slots = [], [], []
    group_records, state_cursor = [], 0
    for gid, state_specs in groups:
        group_records.append((gid, state_cursor, len(state_specs)))
        for sid, target in state_specs:
            states.append((sid, target))
            state_cursor += 1
        if len(state_specs) > 1:
            state_cursor += 1                     # dummy state
    # Resolve each state's index within the flat state table, for target offsets.
    index_of, flat = {}, 0
    for gid, state_specs in groups:
        for sid, _t in state_specs:
            index_of[(gid, sid)] = flat
            flat += 1
        if len(state_specs) > 1:
            flat += 1

    group_count = len(groups)
    state_count = flat
    for gid, state_specs in groups:
        for sid, target in state_specs:
            if target is not None:
                conditions.append((gid, target))
    condition_count = len(conditions)
    pool_count = condition_count + extra_pool_slots

    groups_off = esd.INTERNAL_HEADER_SIZE
    states_off = groups_off + group_count * esd.GROUP_SIZE
    conds_off = states_off + state_count * esd.STATE_SIZE
    calls_off = conds_off + condition_count * esd.CONDITION_SIZE
    args_off = calls_off                                    # no commands
    pool_off = args_off
    blobs_off = pool_off + pool_count * 8
    evaluator = b"\xa1"
    name_off = blobs_off + condition_count * len(evaluator)

    out = bytearray()
    out += struct.pack("<4sIIIII", esd.MAGIC, 1, 3, 3, 0x54, 0)   # dataSize patched later
    out += struct.pack("<I", 6)
    for size, count in ((esd.INTERNAL_HEADER_SIZE, 1), (esd.GROUP_SIZE, group_count),
                        (esd.STATE_SIZE, state_count), (esd.CONDITION_SIZE, condition_count),
                        (esd.COMMAND_CALL_SIZE, 0), (esd.COMMAND_ARG_SIZE, 0)):
        out += struct.pack("<II", size, count)
    name_len = len(name) + 1 if name else 0
    out += struct.pack("<IIII", pool_off, pool_count, name_off, name_len)
    data_end = name_off + name_len * 2
    out += struct.pack("<IIII", data_end, 0, data_end, 0)
    assert len(out) == esd.HEADER_SIZE

    out += struct.pack("<I", 1) + b"\0" * 16 + struct.pack("<I", 0)
    out += struct.pack("<qqqqqq", groups_off, group_count,
                       name_off if name else -1, name_len, -1, -1)

    cursor = 0
    for (gid, first, n), (_gid, state_specs) in zip(group_records, groups):
        off = states_off + first * esd.STATE_SIZE
        out += struct.pack("<qqqq", gid, off, n, off)
    # A state's own condition-pool offset is a bump-allocator cursor, not a
    # sentinel: real ESDs (verified against bossres/melina, byte for byte)
    # give a zero-condition state wherever the cursor happens to stand, never
    # -1. Harmless either way for the reader -- it gates condition reads on
    # the count, never the offset, for exactly this reason -- but write() has
    # to reproduce it to round-trip a real file, so this fixture has to match.
    cond_i = 0
    for gid, state_specs in groups:
        first_co_off = None
        for j, (sid, target) in enumerate(state_specs):
            co_off = pool_off + cond_i * 8
            if j == 0:
                first_co_off = co_off
            if target is None:
                out += struct.pack("<qqqqqqqqq", sid, co_off, 0, -1, 0, -1, 0, -1, 0)
            else:
                out += struct.pack("<qqqqqqqqq", sid, co_off, 1, -1, 0, -1, 0, -1, 0)
                cond_i += 1
        if len(state_specs) > 1:
            first_sid, first_target = state_specs[0]
            first_count = 0 if first_target is None else 1
            out += struct.pack("<qqqqqqqqq", first_sid, first_co_off, first_count,
                               -1, 0, -1, 0, -1, 0)
    for i, (gid, target) in enumerate(conditions):
        target_off = states_off + index_of[(gid, target)] * esd.STATE_SIZE
        out += struct.pack("<qqqqqqq", target_off, -1, 0, -1, 0,
                           blobs_off + i * len(evaluator), len(evaluator))
    for i in range(pool_count):
        # Padding slots point at the first condition row: orphaned, but a
        # valid pointer, which is what vanilla leaves behind.
        row = min(i, condition_count - 1) if condition_count else 0
        out += struct.pack("<q", conds_off + row * esd.CONDITION_SIZE)
    out += evaluator * condition_count
    out += name.encode("utf-16-le") + b"\0\0" if name else b""

    struct.pack_into("<I", out, 0x14, len(out) - esd.HEADER_SIZE)
    return bytes(out)


def test_read_returns_groups_states_and_targets():
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    parsed = esd.read(raw)
    assert [g.id for g in parsed.groups] == [1]
    assert [s.id for s in parsed.groups[0].states] == [0, 1]
    assert parsed.groups[0].states[0].conditions[0].target == 1
    assert parsed.groups[0].states[1].conditions == ()
    assert parsed.name == "t000001000"


def test_read_drops_the_trailing_dummy_state():
    """Each group carries a copy of state 0 after its real states. It is a writer
    artifact, regenerated on write, and must not appear as a real state."""
    raw = _synthetic([(1, [(0, None), (1, None), (2, None)])])
    parsed = esd.read(raw)
    assert [s.id for s in parsed.groups[0].states] == [0, 1, 2]


def test_read_rejects_a_bad_magic():
    raw = bytearray(_synthetic([(1, [(0, None), (1, None)])]))
    raw[:4] = b"XXXX"
    with pytest.raises(esd.EsdError):
        esd.read(bytes(raw))


def test_read_rejects_a_directory_that_does_not_tile_the_file():
    """A count that doesn't match the file length means the layout was misread.
    Better to refuse than to hand back a plausible-looking partial graph."""
    raw = bytearray(_synthetic([(1, [(0, None), (1, None)])]))
    struct.pack_into("<I", raw, 0x14, 999999)
    with pytest.raises(esd.EsdError):
        esd.read(bytes(raw))


def test_read_rejects_a_table_count_that_overflows_the_buffer():
    """dataSize (0x14) and the pool-offset tiling check (0x4C) are both
    self-referential -- an attacker who controls the whole header can inflate
    any table count and still keep both internally consistent, since neither
    one compares a table's extent to the real buffer. Without an explicit
    bounds check, a bad count survives both guards and blows up as a raw
    struct.error deep in a read loop instead of raising EsdError cleanly."""
    raw = bytearray(_synthetic([(1, [(0, None), (1, None)])]))
    inflated_group_count = 999999
    struct.pack_into("<I", raw, 0x28, inflated_group_count)  # group_count field
    state_count = struct.unpack_from("<I", raw, 0x30)[0]
    cond_count = struct.unpack_from("<I", raw, 0x38)[0]
    call_count = struct.unpack_from("<I", raw, 0x40)[0]
    arg_count = struct.unpack_from("<I", raw, 0x48)[0]
    states_at = esd.INTERNAL_HEADER_SIZE + inflated_group_count * esd.GROUP_SIZE
    conds_at = states_at + state_count * esd.STATE_SIZE
    calls_at = conds_at + cond_count * esd.CONDITION_SIZE
    args_at = calls_at + call_count * esd.COMMAND_CALL_SIZE
    pool_at = args_at + arg_count * esd.COMMAND_ARG_SIZE
    # Keep the tiling check happy so only the new bounds check can catch this.
    struct.pack_into("<I", raw, 0x4C, pool_at)
    with pytest.raises(esd.EsdError):
        esd.read(bytes(raw))


def test_read_rejects_a_cross_group_jump_target():
    """Verified zero of these in all three real files. Merging assumes a group is
    self-contained for jumps, so one would silently break the graft."""
    raw = bytearray(_synthetic([(1, [(0, 1), (1, None)]), (2, [(0, None), (1, None)])]))
    conds_off = esd.HEADER_SIZE + esd.INTERNAL_HEADER_SIZE + 2 * esd.GROUP_SIZE \
        + 6 * esd.STATE_SIZE
    # Retarget the only condition at a state in group 2.
    struct.pack_into("<q", raw, conds_off,
                     esd.INTERNAL_HEADER_SIZE + 2 * esd.GROUP_SIZE + 3 * esd.STATE_SIZE)
    with pytest.raises(esd.EsdError):
        esd.read(bytes(raw))


@pytest.mark.parametrize("which", ["vanilla", "bossres", "melina"])
def test_reads_the_real_files_with_the_counts_the_header_declares(which):
    """The oracle. Walking the graph must reach exactly the element counts the
    header states — that is what proves the layout, not that parsing didn't crash."""
    parsed = esd.read(real_esd(which))
    groups, states, conds, calls, args = EXPECTED[which]
    assert len(parsed.groups) == groups
    assert sum(len(g.states) for g in parsed.groups) + len(parsed.groups) == states

    # Vanilla's compiler pools identical condition subtrees: the same physical
    # Condition record can be pointed at from several states' pointer-pool
    # entries (verified directly against the bytes -- e.g. states 10-13 of one
    # group all point at the same offset). A naive walk revisits it once per
    # pointer, so identity (`order`, the record's fixed table index) is what
    # tells a genuine revisit apart from a misparse that invents extra nodes.
    seen_conds_raw = [c for g in parsed.groups for s in g.states
                      for c in _walk_conditions(s.conditions)]
    seen_conds = list({c.order: c for c in seen_conds_raw}.values())
    assert len(seen_conds) == conds
    seen_calls_raw = [cc for g in parsed.groups for s in g.states
                      for cc in (s.entry + s.exit + s.while_)] + \
                     [cc for c in seen_conds for cc in c.pass_commands]
    seen_calls = list({cc.order: cc for cc in seen_calls_raw}.values())
    assert len(seen_calls) == calls
    assert sum(len(cc.args) for cc in seen_calls) == args


def test_write_round_trips_a_synthetic_file():
    raw = _synthetic([(1, [(0, 1), (1, None)]), (2, [(0, None), (1, 0)])])
    assert esd.write(esd.read(raw)) == raw


@pytest.mark.parametrize("which", ["bossres", "melina"])
def test_write_round_trips_the_mod_files_byte_for_byte(which):
    """The faithfulness gate, and the same bar bnd4 and tpf are held to. These two
    are the class of file the merge actually produces."""
    raw = real_esd(which)
    assert esd.write(esd.read(raw)) == raw


def test_write_round_trips_vanilla_semantically():
    """Not byte-exact, deliberately. Vanilla over-declares its condition-offset pool
    by 128 slots -- runs written for the dummy states and then orphaned -- and erm
    never writes vanilla, only reads it as the merge base. Parsing what we wrote must
    still give back the same graph."""
    raw = real_esd("vanilla")
    once = esd.read(raw)
    rewritten = esd.write(once)

    # Vanilla shares 120 condition records across 2+ states each, and the sharing
    # nests -- 38 of those shared rows only show up inside a subcondition. Dedupe
    # keyed on object identity (or no dedupe at all) gives every duplicate
    # reference its own physical row: the table inflates (1092 -> 1398 on this
    # file) while every value stays semantically correct, since the duplicated
    # rows carry identical content. The state/target/evaluator checks below
    # can't see that -- pin the declared row count directly.
    assert _condition_count(rewritten) == _condition_count(raw)

    twice = esd.read(rewritten)
    assert [g.id for g in twice.groups] == [g.id for g in once.groups]
    for a, b in zip(once.groups, twice.groups):
        assert [s.id for s in a.states] == [s.id for s in b.states]
        for sa, sb in zip(a.states, b.states):
            wa = list(_walk_conditions(sa.conditions))
            wb = list(_walk_conditions(sb.conditions))
            assert [c.target for c in wa] == [c.target for c in wb]
            assert [c.evaluator for c in wa] == [c.evaluator for c in wb]


def test_write_emits_the_pool_size_it_used_not_the_one_the_source_declared():
    """The written header has to describe the pool write() actually laid out.
    A source file can declare more slots than its graph reaches -- vanilla
    over-declares by 128 -- and carrying that number through would leave the
    header promising slots the bytes do not hold, which is a reader's only
    bound on the pool."""
    padding = 128
    raw = _synthetic([(1, [(0, 1), (1, None)])], extra_pool_slots=padding)
    reached = _declared_pool_count(raw) - padding

    rewritten = esd.write(esd.read(raw))
    assert _declared_pool_count(rewritten) == reached

    # And the graph survives the shrink: the orphaned slots carried nothing.
    out = esd.read(rewritten)
    assert [s.id for s in out.groups[0].states] == [0, 1]
    assert out.groups[0].states[0].conditions[0].target == 1


def test_write_appends_a_new_state_and_keeps_every_target_resolvable():
    """Inserting a state shifts the whole state table, so every jump target moves.
    A writer that patched offsets rather than re-deriving them would corrupt these."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    parsed = esd.read(raw)
    group = parsed.groups[0]
    grown = group._replace(states=group.states + (
        esd.State(id=7, conditions=(esd.Condition(target=0, evaluator=b"\xa1"),)),))
    out = esd.read(esd.write(parsed._replace(groups=(grown,))))
    assert [s.id for s in out.groups[0].states] == [0, 1, 7]
    assert out.groups[0].states[2].conditions[0].target == 0
    assert out.groups[0].states[0].conditions[0].target == 1


def test_write_refuses_a_fresh_call_appended_to_an_existing_states_entry():
    """A merge that appends a call to a state whose entry already holds a
    source call, without relaying out the whole call table, produces a run
    that is not contiguous: the fresh call lands at the tail of the table
    with every other grafted record, not next to the state it belongs to.
    `read_calls` recovers a run by walking forward from its first slot, so a
    writer that trusted the first slot here would hand back whichever call
    happens to occupy the neighbouring row instead of the real one -- silently.
    """
    call_a = esd.CommandCall(bank=1, id=100, order=0)
    call_b = esd.CommandCall(bank=1, id=200, order=1)
    fresh = esd.CommandCall(bank=1, id=300)                  # order=None
    state_a = esd.State(id=0, entry=(call_a, fresh), order=0)
    state_b = esd.State(id=1, entry=(call_b,), order=1)
    group = esd.StateGroup(id=1, states=(state_a, state_b), order=0)
    archive = esd.Esd(groups=(group,), name="", unk=(0, 0, 0, 0))
    with pytest.raises(esd.EsdError):
        esd.write(archive)


def test_write_refuses_a_fresh_arg_appended_to_an_existing_calls_args():
    """Same hazard, one level down: a call's own args are a flat run in the
    arg table, addressed by a running cursor rather than a lookup. Appending
    a fresh arg to a call that already has a source arg puts the fresh one at
    the tail of the table instead of next to its call."""
    arg_a = esd.CommandArg(bytecode=b"\x01", order=0)
    arg_b = esd.CommandArg(bytecode=b"\x02", order=1)
    fresh_arg = esd.CommandArg(bytecode=b"\x03")             # order=None
    call_a = esd.CommandCall(bank=1, id=100, args=(arg_a, fresh_arg), order=0)
    call_b = esd.CommandCall(bank=1, id=200, args=(arg_b,), order=1)
    state = esd.State(id=0, entry=(call_a, call_b), order=0)
    group = esd.StateGroup(id=1, states=(state,), order=0)
    archive = esd.Esd(groups=(group,), name="", unk=(0, 0, 0, 0))
    with pytest.raises(esd.EsdError):
        esd.write(archive)


def test_write_keeps_working_for_a_fresh_condition_on_an_existing_state():
    """Conditions don't share the calls/args hazard: the condition pool is an
    indirection table, and a state's own reservation covers its whole
    condition list in one shot regardless of how many of those conditions are
    freshly created, so appending one to an existing state's list needs no
    relayout and must not be refused."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    parsed = esd.read(raw)
    group = parsed.groups[0]
    state0 = group.states[0]
    grown_state0 = state0._replace(
        conditions=state0.conditions + (esd.Condition(target=1, evaluator=b"\xa2"),))
    grown_group = group._replace(states=(grown_state0,) + group.states[1:])
    out = esd.read(esd.write(parsed._replace(groups=(grown_group,))))
    assert [c.target for c in out.groups[0].states[0].conditions] == [1, 1]


def test_write_keeps_working_for_a_wholly_fresh_state_with_calls_and_args():
    """A brand new state with its own brand new calls and args has nothing to
    be non-contiguous with -- everything about it is fresh and lands together
    at the tail of every table."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    parsed = esd.read(raw)
    group = parsed.groups[0]
    fresh_arg = esd.CommandArg(bytecode=b"\x09")
    fresh_call = esd.CommandCall(bank=1, id=42, args=(fresh_arg,))
    fresh_state = esd.State(id=7, entry=(fresh_call,))
    grown = group._replace(states=group.states + (fresh_state,))
    out = esd.read(esd.write(parsed._replace(groups=(grown,))))
    new_state = out.groups[0].states[-1]
    assert new_state.id == 7
    assert (new_state.entry[0].bank, new_state.entry[0].id) == (1, 42)
    assert new_state.entry[0].args[0].bytecode == b"\x09"


def test_write_refuses_two_different_conditions_sharing_an_order():
    """`.order` is only unique within the file it came from. If a merge ever
    forwards a grafted record's original `.order` instead of stamping it
    order=None, two conditions from different source files can collide on
    the same table index while being genuinely different records -- and
    silently keeping one and dropping the other produces a graph that looks
    fine and is quietly wrong."""
    cond_a = esd.Condition(target=None, evaluator=b"\xa1", order=5)
    cond_b = esd.Condition(target=None, evaluator=b"\xa2", order=5)
    state = esd.State(id=0, conditions=(cond_a, cond_b), order=0)
    group = esd.StateGroup(id=1, states=(state,), order=0)
    archive = esd.Esd(groups=(group,), name="", unk=(0, 0, 0, 0))
    with pytest.raises(esd.EsdError):
        esd.write(archive)


@pytest.mark.parametrize("which", ["vanilla", "bossres", "melina"])
def test_write_does_not_mistake_genuine_sharing_for_an_order_collision(which):
    """The collision guard in `_dedupe` must never fire on an actual file: a
    genuinely shared record is content-identical at every reference by
    construction (it's the same physical row, re-read fresh each time), so a
    plain read-then-write of any real file has to pass with no EsdError."""
    esd.write(esd.read(real_esd(which)))


def test_write_refuses_a_jump_target_the_group_does_not_have():
    """A merge that renumbers states and misses a reference gets here. Before,
    it came out as a bare KeyError -- erm catches ErmError and nothing else, so
    a stale jump target reached the user as a stack trace."""
    state = esd.State(0, conditions=(esd.Condition(target=99, evaluator=b"\x41\xa1"),))
    archive = esd.Esd(groups=(esd.StateGroup(1, (state,)),), name="",
                      unk=(0, 0, 0, 0))
    with pytest.raises(esd.EsdError, match="99"):
        esd.write(archive)


def test_write_refuses_a_group_with_no_states():
    """The group header has to point at a first state row, and there isn't
    one. IndexError said nothing about which group or why."""
    archive = esd.Esd(groups=(esd.StateGroup(1, ()),), name="",
                      unk=(0, 0, 0, 0))
    with pytest.raises(esd.EsdError, match="no states"):
        esd.write(archive)


def test_first_state_is_the_lowest_order_row_not_the_first_in_the_tuple():
    """Where a machine starts is one rule, and two callers read it: the writer,
    which points the group header at a row, and the merge's reachability walk.
    They only agree by accident on a group that came off disk -- a merge can
    hand back states in an order that is not their table order, and then a walk
    that trusts tuple position starts somewhere the game never enters.
    """
    late = esd.State(7, order=3)
    early = esd.State(4, order=1)
    group = esd.StateGroup(1, (late, early))
    assert esd.first_state(group) is early


def test_first_state_falls_back_to_tuple_order_for_fresh_records():
    """Merge-created rows carry order=None and are appended in the order they
    were added, so the first of those is the entry when nothing is numbered."""
    group = esd.StateGroup(1, (esd.State(4), esd.State(7)))
    assert esd.first_state(group).id == 4


def test_write_points_the_group_header_at_the_lowest_order_state():
    """The rule has to be the one the file actually carries, not just what the
    helper returns: reading back gives the states in table order."""
    group = esd.StateGroup(1, (esd.State(7, order=3), esd.State(4, order=1)))
    archive = esd.Esd(groups=(group,), name="t000001000",
                      unk=(0, 0, 0, 0))
    reread = esd.read(esd.write(archive))
    assert [s.id for s in reread.groups[0].states] == [4, 7]


def test_a_single_state_group_round_trips_without_a_dummy_row():
    """Every group in the three real files has at least two states, so the
    branch that skips the dummy row is never taken by any file on disk."""
    group = esd.StateGroup(1, (esd.State(0, conditions=(
        esd.Condition(target=0, evaluator=b"\x41\xa1"),)),))
    archive = esd.Esd(groups=(group,), name="t000001000",
                      unk=(0, 0, 0, 0))
    raw = esd.write(archive)
    reread = esd.read(raw)
    assert [s.id for s in reread.groups[0].states] == [0]
    assert reread.groups[0].states[0].conditions[0].target == 0
    assert esd.write(reread) == raw


def _corrupt(raw, offset, value, fmt="<I"):
    out = bytearray(raw)
    struct.pack_into(fmt, out, offset, value)
    return bytes(out)


def test_read_refuses_a_name_that_runs_past_the_end():
    """Slicing bytes past the end gives a short result instead of raising, so a
    bad name_off/name_len used to come back as a silently truncated name."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    with pytest.raises(esd.EsdError, match="name"):
        esd.read(_corrupt(raw, 0x58, 4000))                # name_len
    with pytest.raises(esd.EsdError, match="name"):
        esd.read(_corrupt(raw, 0x54, 0x7000_0000))         # name_off


def test_read_refuses_a_state_run_that_leaves_its_table():
    """The header checks bound the five tables against the buffer; they say
    nothing about where a run inside one starts. A group claiming states from
    beyond the state table used to read whatever bytes sat there."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    group_at = esd.HEADER_SIZE + esd.INTERNAL_HEADER_SIZE
    states_at = esd.INTERNAL_HEADER_SIZE + esd.GROUP_SIZE
    with pytest.raises(esd.EsdError, match="state run"):
        esd.read(_corrupt(raw, group_at + 8,
                          states_at + 40 * esd.STATE_SIZE, "<q"))


def test_read_refuses_a_state_run_that_is_not_on_a_row_boundary():
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    group_at = esd.HEADER_SIZE + esd.INTERNAL_HEADER_SIZE
    states_at = esd.INTERNAL_HEADER_SIZE + esd.GROUP_SIZE
    with pytest.raises(esd.EsdError, match="row boundary"):
        esd.read(_corrupt(raw, group_at + 8, states_at + 3, "<q"))


def test_read_refuses_a_condition_pool_run_outside_the_pool():
    """A state's conditions are addressed by an offset into the pool, and the
    pool is the one region whose declared length vanilla overstates -- so it
    gets bounded against the file rather than against that count."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    states_at = esd.HEADER_SIZE + esd.INTERNAL_HEADER_SIZE + esd.GROUP_SIZE
    with pytest.raises(esd.EsdError, match="condition pool"):
        esd.read(_corrupt(raw, states_at + 8, 0x7000_0000, "<q"))


def test_read_refuses_an_evaluator_that_runs_past_the_end():
    """A truncated evaluator decodes to a different machine, which is the one
    outcome the canonicaliser has no way to notice."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    conds_at = (esd.HEADER_SIZE + esd.INTERNAL_HEADER_SIZE + esd.GROUP_SIZE
                + 3 * esd.STATE_SIZE)
    with pytest.raises(esd.EsdError, match="evaluator"):
        esd.read(_corrupt(raw, conds_at + 48, 4000, "<q"))     # ev_len


def test_read_refuses_a_condition_that_is_its_own_subcondition():
    """A subcondition pool slot pointing back at an ancestor row recurses until
    CPython's frame limit. RecursionError is not an ErmError, so it reaches the
    user as a stack trace instead of the named refusal every other malformed ESD
    gets. Ancestors only, not every row seen: shipped files really do reference
    one condition row from several pools."""
    raw = _synthetic([(1, [(0, 1), (1, None)])])
    conds_rel = esd.INTERNAL_HEADER_SIZE + esd.GROUP_SIZE + 3 * esd.STATE_SIZE
    conds_at = esd.HEADER_SIZE + conds_rel
    # Slot 0 of the pool already holds condition 0's own row offset.
    raw = _corrupt(raw, conds_at + 24, conds_rel + esd.CONDITION_SIZE, "<q")
    raw = _corrupt(raw, conds_at + 32, 1, "<q")
    with pytest.raises(esd.EsdError, match="cycle"):
        esd.read(raw)
