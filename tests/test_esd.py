import struct

import pytest

from ermlib.formats import esd
from tests.esd_fixtures import EXPECTED, real_esd


def _synthetic(groups, name="t000001000"):
    """Build a minimal valid ESD. groups: [(group_id, [(state_id, target_or_None)])].

    Deliberately hand-rolled rather than going through esd.write, so the reader is
    tested against bytes assembled independently of it.
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
    pool_count = condition_count

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
    cond_i = 0
    for gid, state_specs in groups:
        for sid, target in state_specs:
            if target is None:
                out += struct.pack("<qqqqqqqqq", sid, -1, 0, -1, 0, -1, 0, -1, 0)
            else:
                out += struct.pack("<qqqqqqqqq", sid, pool_off + cond_i * 8, 1,
                                   -1, 0, -1, 0, -1, 0)
                cond_i += 1
        if len(state_specs) > 1:
            first_sid, first_target = state_specs[0]
            if first_target is None:
                out += struct.pack("<qqqqqqqqq", first_sid, -1, 0, -1, 0, -1, 0, -1, 0)
            else:
                out += struct.pack("<qqqqqqqqq", first_sid, pool_off, 1,
                                   -1, 0, -1, 0, -1, 0)
    for i, (gid, target) in enumerate(conditions):
        target_off = states_off + index_of[(gid, target)] * esd.STATE_SIZE
        out += struct.pack("<qqqqqqq", target_off, -1, 0, -1, 0,
                           blobs_off + i * len(evaluator), len(evaluator))
    for i in range(pool_count):
        out += struct.pack("<q", conds_off + i * esd.CONDITION_SIZE)
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

    def walk_conditions(cs):
        for c in cs:
            yield c
            yield from walk_conditions(c.subconditions)

    # Vanilla's compiler pools identical condition subtrees: the same physical
    # Condition record can be pointed at from several states' pointer-pool
    # entries (verified directly against the bytes -- e.g. states 10-13 of one
    # group all point at the same offset). A naive walk revisits it once per
    # pointer, so identity (`order`, the record's fixed table index) is what
    # tells a genuine revisit apart from a misparse that invents extra nodes.
    seen_conds_raw = [c for g in parsed.groups for s in g.states
                      for c in walk_conditions(s.conditions)]
    seen_conds = list({c.order: c for c in seen_conds_raw}.values())
    assert len(seen_conds) == conds
    seen_calls_raw = [cc for g in parsed.groups for s in g.states
                      for cc in (s.entry + s.exit + s.while_)] + \
                     [cc for c in seen_conds for cc in c.pass_commands]
    seen_calls = list({cc.order: cc for cc in seen_calls_raw}.values())
    assert len(seen_calls) == calls
    assert sum(len(cc.args) for cc in seen_calls) == args
