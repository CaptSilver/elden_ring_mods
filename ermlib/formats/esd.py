"""ESD ("EZState"): the compiled state machines behind dialogue and grace menus.

A graph, unlike every other format here. Conditions carry jump targets that name a
state by byte offset, so a state cannot be lifted from one file to another the way a
param row or an FMG string can -- the references have to be rebuilt. That is why the
reader resolves targets to state ids and the writer re-derives offsets, and why
nothing ever patches an offset in place.

Records keep the index they had in their source table (`order`). The writer emits in
ascending original order so a round-trip reproduces the source layout exactly, without
having to reverse-engineer FromSoft's emission rule -- which nobody has. Records
created by a merge carry `order = None` and are appended after.
"""
import struct
from typing import NamedTuple, Optional

from ..errors import ErmError

MAGIC = b"fsSL"
HEADER_SIZE = 0x6C
INTERNAL_HEADER_SIZE = 72
GROUP_SIZE = 32
STATE_SIZE = 72
CONDITION_SIZE = 56
COMMAND_CALL_SIZE = 24
COMMAND_ARG_SIZE = 16
# Bank 6 command ids name a state group -- a cross-group call. Banks 1/5/7 never
# collide with group ids, so this is the only bank a regraft has to rewrite.
CALL_BANK = 6


class EsdError(ErmError):
    """An ESD was malformed, or used a layout this reader refuses to guess at."""


class CommandArg(NamedTuple):
    bytecode: bytes
    order: Optional[int] = None
    blob_order: Optional[int] = None


class CommandCall(NamedTuple):
    bank: int
    id: int
    args: tuple = ()
    order: Optional[int] = None


class Condition(NamedTuple):
    target: Optional[int]
    pass_commands: tuple = ()
    subconditions: tuple = ()
    evaluator: bytes = b""
    order: Optional[int] = None
    blob_order: Optional[int] = None


class State(NamedTuple):
    id: int
    conditions: tuple = ()
    entry: tuple = ()
    exit: tuple = ()
    while_: tuple = ()
    order: Optional[int] = None


class StateGroup(NamedTuple):
    id: int
    states: tuple
    order: Optional[int] = None


class Esd(NamedTuple):
    groups: tuple
    name: str
    unk: tuple            # the four u32s at internal header +4, meaning unknown
    pool_count: int       # as declared; vanilla over-declares, see the design doc


def _u32(data, off):
    return struct.unpack_from("<I", data, off)[0]


def _i64(data, off):
    return struct.unpack_from("<q", data, off)[0]


def read(data):
    """Parse an ESD. Raises EsdError rather than returning a partial graph."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise EsdError("not an ESD (bad magic)")
    data_size = _u32(data, 0x14)
    if HEADER_SIZE + data_size != len(data):
        raise EsdError(
            f"ESD header declares {data_size} data bytes but the file holds "
            f"{len(data) - HEADER_SIZE} -- the layout was misread")
    descriptors = [(_u32(data, 0x1C + i * 8), _u32(data, 0x20 + i * 8)) for i in range(6)]
    (_ih_size, _ih_n), (_g_size, group_count), (_s_size, state_count), \
        (_c_size, cond_count), (_cc_size, call_count), (_ca_size, arg_count) = descriptors
    pool_count = _u32(data, 0x50)
    name_off, name_len = _u32(data, 0x54), _u32(data, 0x58)

    # Table bases, data-relative. The regions are contiguous in this order.
    groups_at = INTERNAL_HEADER_SIZE
    states_at = groups_at + group_count * GROUP_SIZE
    conds_at = states_at + state_count * STATE_SIZE
    calls_at = conds_at + cond_count * CONDITION_SIZE
    args_at = calls_at + call_count * COMMAND_CALL_SIZE
    pool_at = args_at + arg_count * COMMAND_ARG_SIZE

    # The data_size check above and the 0x4C check below are both
    # self-referential: they compare header fields to each other, and an
    # attacker who controls the whole header can keep them mutually
    # consistent while inflating any of the five counts far past what the
    # file actually holds. Neither one bounds a table against the real
    # buffer, so a bad count would otherwise survive both checks and blow up
    # as a raw struct.error deep in a read loop below instead of failing
    # cleanly here. These files come from the same untrusted vendor/mod
    # pipeline as BND4 and TPF, so every count gets the same treatment.
    data_len = len(data) - HEADER_SIZE
    for label, count, end in (
        ("state group", group_count, states_at),
        ("state", state_count, conds_at),
        ("condition", cond_count, calls_at),
        ("command call", call_count, args_at),
        ("command arg", arg_count, pool_at),
    ):
        if end > data_len:
            raise EsdError(
                f"{label} table (count={count}) runs past the end of the "
                f"{len(data)}-byte file")

    if _u32(data, 0x4C) != pool_at:
        raise EsdError(
            f"condition-offset pool is at {_u32(data, 0x4C)} but the tables end at "
            f"{pool_at} -- the tables do not tile the file")

    def abs_(rel):
        return HEADER_SIZE + rel

    unk = struct.unpack_from("<IIII", data, abs_(4))
    name = ""
    if name_len:
        raw = data[abs_(name_off):abs_(name_off) + (name_len - 1) * 2]
        name = raw.decode("utf-16-le")

    def read_args(offset, count):
        # Gate on the count. The offset is -1 in some writers and a live cursor in
        # others for an empty list, so it can never be the emptiness test.
        out = []
        for i in range(count):
            idx = (offset - args_at) // COMMAND_ARG_SIZE + i
            at = abs_(args_at + idx * COMMAND_ARG_SIZE)
            b_off, b_len = _i64(data, at), _i64(data, at + 8)
            out.append(CommandArg(data[abs_(b_off):abs_(b_off) + b_len], idx, b_off))
        return tuple(out)

    def read_calls(offset, count):
        out = []
        for i in range(count):
            idx = (offset - calls_at) // COMMAND_CALL_SIZE + i
            at = abs_(calls_at + idx * COMMAND_CALL_SIZE)
            bank, cid = struct.unpack_from("<ii", data, at)
            a_off, a_count = _i64(data, at + 8), _i64(data, at + 16)
            out.append(CommandCall(bank, cid, read_args(a_off, a_count), idx))
        return tuple(out)

    def read_conditions(pool_offset, count):
        out = []
        for i in range(count):
            cond_rel = _i64(data, abs_(pool_offset + i * 8))
            idx = (cond_rel - conds_at) // CONDITION_SIZE
            at = abs_(conds_at + idx * CONDITION_SIZE)
            target = _i64(data, at)
            pc_off, pc_count = _i64(data, at + 8), _i64(data, at + 16)
            sc_off, sc_count = _i64(data, at + 24), _i64(data, at + 32)
            ev_off, ev_len = _i64(data, at + 40), _i64(data, at + 48)
            out.append(Condition(
                target,                       # still an offset; resolved below
                read_calls(pc_off, pc_count),
                read_conditions(sc_off, sc_count),
                data[abs_(ev_off):abs_(ev_off) + ev_len],
                idx, ev_off))
        return tuple(out)

    groups, state_index = [], {}
    for gi in range(group_count):
        at = abs_(groups_at + gi * GROUP_SIZE)
        gid, states_off, n = _i64(data, at), _i64(data, at + 8), _i64(data, at + 16)
        first = (states_off - states_at) // STATE_SIZE
        states = []
        for si in range(n):
            idx = first + si
            sat = abs_(states_at + idx * STATE_SIZE)
            sid = _i64(data, sat)
            co_off, co_count = _i64(data, sat + 8), _i64(data, sat + 16)
            en_off, en_count = _i64(data, sat + 24), _i64(data, sat + 32)
            ex_off, ex_count = _i64(data, sat + 40), _i64(data, sat + 48)
            wh_off, wh_count = _i64(data, sat + 56), _i64(data, sat + 64)
            states.append(State(sid, read_conditions(co_off, co_count),
                                read_calls(en_off, en_count),
                                read_calls(ex_off, ex_count),
                                read_calls(wh_off, wh_count), idx))
            state_index[states_at + idx * STATE_SIZE] = (gid, sid)
        groups.append(StateGroup(gid, tuple(states), gi))

    # Second pass: a target may name a state parsed after its own condition, so
    # offsets can only be resolved once every state's position is known.
    def resolve(group_id, conditions):
        out = []
        for c in conditions:
            if c.target == -1:
                target = None
            else:
                owner = state_index.get(c.target)
                if owner is None:
                    raise EsdError(
                        f"condition jump target {c.target} does not name a state")
                if owner[0] != group_id:
                    raise EsdError(
                        f"condition in group {group_id} jumps to group {owner[0]} -- "
                        f"cross-group jumps are unsupported, and none exist in any "
                        f"shipped file, so this is more likely a misparse")
                target = owner[1]
            out.append(c._replace(target=target,
                                  subconditions=resolve(group_id, c.subconditions)))
        return tuple(out)

    groups = tuple(
        g._replace(states=tuple(
            s._replace(conditions=resolve(g.id, s.conditions)) for s in g.states))
        for g in groups)
    return Esd(groups, name, unk, pool_count)
