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

    # Everything below this point is addressed by numbers the file itself
    # supplies: a run is (byte offset, count) and the reader recovers the rest
    # by walking forward. The header checks above bound the five tables against
    # the buffer but say nothing about where a run inside them starts, so
    # without these two an offset that is unaligned, before its table, or short
    # of the rows it claims reads whatever bytes happen to sit there -- or
    # surfaces as a raw struct.error several frames down instead of an EsdError.
    def run_start(what, offset, base, size, count, n):
        """The first row index of a run, or a refusal."""
        if n <= 0:
            return 0
        if offset < base or (offset - base) % size:
            raise EsdError(
                f"{what} run starts at {offset}, which is not a row boundary of "
                f"the table at {base}")
        first = (offset - base) // size
        if first + n > count:
            raise EsdError(
                f"{what} run of {n} at row {first} runs past the end of the "
                f"{count}-row table")
        return first

    def blob(offset, length, what):
        """A payload slice. Slicing `bytes` past the end silently gives a short
        result, which turns a corrupt offset into a truncated evaluator that
        decodes to a different machine."""
        if length <= 0:
            return b""
        if offset < 0 or offset + length > data_len:
            raise EsdError(
                f"{what} at {offset} ({length} bytes) runs past the end of the "
                f"{len(data)}-byte file")
        return data[abs_(offset):abs_(offset) + length]

    unk = struct.unpack_from("<IIII", data, abs_(4))
    name = ""
    if name_len:
        raw = blob(name_off, (name_len - 1) * 2, "name")   # the NUL isn't stored
        try:
            name = raw.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise EsdError(f"ESD name at {name_off} is not UTF-16: {exc}") from exc

    def read_args(offset, count):
        # Gate on the count. The offset is -1 in some writers and a live cursor in
        # others for an empty list, so it can never be the emptiness test.
        out = []
        first = run_start("command arg", offset, args_at, COMMAND_ARG_SIZE,
                          arg_count, count)
        for i in range(count):
            idx = first + i
            at = abs_(args_at + idx * COMMAND_ARG_SIZE)
            b_off, b_len = _i64(data, at), _i64(data, at + 8)
            out.append(CommandArg(blob(b_off, b_len, "command arg bytecode"),
                                  idx, b_off))
        return tuple(out)

    def read_calls(offset, count):
        out = []
        first = run_start("command call", offset, calls_at, COMMAND_CALL_SIZE,
                          call_count, count)
        for i in range(count):
            idx = first + i
            at = abs_(calls_at + idx * COMMAND_CALL_SIZE)
            bank, cid = struct.unpack_from("<ii", data, at)
            a_off, a_count = _i64(data, at + 8), _i64(data, at + 16)
            out.append(CommandCall(bank, cid, read_args(a_off, a_count), idx))
        return tuple(out)

    def read_conditions(pool_offset, count, stack=()):
        out = []
        # The pool is bounded against the buffer rather than against its
        # declared slot count: vanilla over-declares that, so trusting it would
        # reject FromSoft's own file.
        if count > 0 and (pool_offset < pool_at or (pool_offset - pool_at) % 8
                          or pool_offset + count * 8 > data_len):
            raise EsdError(
                f"condition pool run of {count} at {pool_offset} is not inside "
                f"the pool at {pool_at}")
        for i in range(count):
            cond_rel = _i64(data, abs_(pool_offset + i * 8))
            idx = run_start("condition", cond_rel, conds_at, CONDITION_SIZE,
                            cond_count, 1)
            # Ancestors only, never every row seen: shipped files reference one
            # condition row from several pools, and _dedupe exists because of
            # it. A row reached from its own subcondition pool is the case
            # nothing else stops -- it recurses to CPython's frame limit and
            # surfaces as a RecursionError instead of an EsdError.
            if idx in stack:
                raise EsdError(
                    "condition cycle: " + " -> ".join(str(i) for i in stack + (idx,)))
            at = abs_(conds_at + idx * CONDITION_SIZE)
            target = _i64(data, at)
            pc_off, pc_count = _i64(data, at + 8), _i64(data, at + 16)
            sc_off, sc_count = _i64(data, at + 24), _i64(data, at + 32)
            ev_off, ev_len = _i64(data, at + 40), _i64(data, at + 48)
            out.append(Condition(
                target,                       # still an offset; resolved below
                read_calls(pc_off, pc_count),
                read_conditions(sc_off, sc_count, stack + (idx,)),
                blob(ev_off, ev_len, "condition evaluator"),
                idx, ev_off))
        return tuple(out)

    groups, state_index = [], {}
    for gi in range(group_count):
        at = abs_(groups_at + gi * GROUP_SIZE)
        gid, states_off, n = _i64(data, at), _i64(data, at + 8), _i64(data, at + 16)
        first = run_start("state", states_off, states_at, STATE_SIZE, state_count, n)
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
    return Esd(groups, name, unk)


def _ordered(records):
    """Source order first, then anything a merge created, in the order it was added.

    Reproducing a file's own layout is what makes a byte-exact round-trip possible
    without knowing how the original writer chose to lay things out -- FromSoft's
    rule for interleaving bytecode blobs has never been recovered, and this sidesteps
    needing it.
    """
    known = [r for r in records if r.order is not None]
    fresh = [r for r in records if r.order is None]
    return sorted(known, key=lambda r: r.order) + fresh


def first_state(group):
    """The state a group's header points at -- where the machine starts.

    One rule, in one place, because two callers need it and disagreeing is
    silent: `write` emits a group's states in `_ordered` order and points the
    header at the first row, so anything walking the graph has to start where
    the game will. Tuple order agrees for any group that came off disk (a read
    hands states back in table order) and can differ for one a merge assembled.
    """
    if not group.states:
        raise EsdError(
            f"state group {group.id} has no states -- a machine with no entry "
            f"point cannot be written or walked")
    return _ordered(group.states)[0]


def _dedupe(records):
    """Collapse repeated references to one physically-shared record to a single
    representative.

    Conditions can be shared: several states' condition-pool entries -- or several
    conditions' subcondition pools -- pointing at the identical table row. The
    reader has no way to notice this and re-reads the row fresh every time it is
    referenced, so it hands back a distinct object per reference, all stamped with
    the shared row's `.order`. The same thing happens one level down: a shared
    condition's pass_commands (and their args) get re-read fresh at each
    reference too, so command calls and args need the same treatment. `order` (the
    row's fixed table index) is what tells a genuine duplicate reference apart
    from two distinct records -- `id()` gives every duplicate its own slot and
    fans a shared row out into copies on write.

    Records with `order is None` were created after the file was read (by a
    merge) and are never coalesced with each other -- each occurrence is a
    distinct new record.

    `order` is only unique within the file it came from. Merging two files
    means two condition (or call, or arg) records that were never the same
    physical row can carry the same `order`, purely by coincidence of table
    position in their own separate files. Genuinely shared records are
    content-identical by construction (they are re-reads of one physical
    row), so two records at the same `order` that differ in any other field
    are exactly that collision -- silently keeping one and dropping the
    other would produce a graph that looks fine and is quietly wrong. A
    merge is expected to head this off by stamping grafted records with
    `order=None` before they ever reach here; this is the backstop for when
    that is missed, not the primary fix.
    """
    seen, out = {}, []
    for r in records:
        key = r.order if r.order is not None else id(r)
        if key not in seen:
            seen[key] = r
            out.append(r)
        elif seen[key] != r:
            raise EsdError(
                f"two different records share table index {key} -- likely a "
                f"merge of two files whose `.order` numbering collided; "
                f"grafted records must carry order=None")
    return out


def _slots(occurrences, deduped):
    """Map every occurrence -- including duplicate references to a shared record
    -- to the table slot its representative ends up in within `deduped`."""
    index = {}
    for i, r in enumerate(deduped):
        index[r.order if r.order is not None else id(r)] = i
    return {id(r): index[r.order if r.order is not None else id(r)] for r in occurrences}


def write(archive):
    """Serialise an Esd. Reproduces a file the reader read, byte for byte."""
    groups = _ordered(archive.groups)
    entry = {group.id: first_state(group) for group in groups}

    # Flatten, keeping every reference encountered -- including duplicates of a
    # shared record -- so `_dedupe` sees the full picture and `_slots` can answer
    # for any occurrence a nested walk hands it. `cond_group` records which
    # top-level group a condition belongs to as it is visited, which is the only
    # way to answer that cheaply later: a condition's own fields don't say, and
    # a shared condition's target must in any case lie in the group of every
    # state that references it (the reader rejects cross-group jumps), so one
    # answer per condition is enough regardless of how many states share it.
    all_conditions, all_calls, all_args = [], [], []
    cond_group = {}

    def collect_calls(calls):
        for call in calls:
            all_calls.append(call)
            all_args.extend(call.args)

    def collect_conditions(conditions, group_id):
        for cond in conditions:
            all_conditions.append(cond)
            cond_group[id(cond)] = group_id
            collect_calls(cond.pass_commands)
            collect_conditions(cond.subconditions, group_id)

    for group in groups:
        for state in _ordered(group.states):
            collect_conditions(state.conditions, group.id)
            collect_calls(state.entry)
            collect_calls(state.exit)
            collect_calls(state.while_)

    calls = _ordered(_dedupe(all_calls))
    args = _ordered(_dedupe(all_args))
    conditions = _ordered(_dedupe(all_conditions))
    call_slot = _slots(all_calls, calls)
    arg_slot = _slots(all_args, args)
    cond_slot = _slots(all_conditions, conditions)

    # State table: each group's states, then one dummy copy of state 0 when the group
    # holds more than one. Position is what a jump target names, so this is fixed
    # before anything that references a state is emitted.
    state_slot, state_rows, slot = {}, [], 0
    for group in groups:
        ordered_states = _ordered(group.states)
        for state in ordered_states:
            state_slot[(group.id, state.id)] = slot
            state_rows.append((group.id, state))
            slot += 1
        if len(ordered_states) > 1:
            state_rows.append((group.id, entry[group.id]))       # dummy
            slot += 1
    state_count = slot

    group_count = len(groups)
    groups_at = INTERNAL_HEADER_SIZE
    states_at = groups_at + group_count * GROUP_SIZE
    conds_at = states_at + state_count * STATE_SIZE
    calls_at = conds_at + len(conditions) * CONDITION_SIZE
    args_at = calls_at + len(calls) * COMMAND_CALL_SIZE
    pool_at = args_at + len(args) * COMMAND_ARG_SIZE

    # The pool holds one slot per state->condition and per condition->subcondition
    # link, in the order the graph is walked. A state whose conditions are shared
    # with another state still gets its own reservation here -- only the *value*
    # written into it (the condition's slot, via cond_slot) is shared.
    pool, pool_start = [], {}

    def reserve(conds, key):
        pool_start[key] = pool_at + len(pool) * 8
        for cond in conds:
            pool.append(conds_at + cond_slot[id(cond)] * CONDITION_SIZE)

    for group in groups:
        for state in _ordered(group.states):
            reserve(state.conditions, ("state", group.id, state.id))
    for cond in conditions:
        if cond.subconditions:
            reserve(cond.subconditions, ("cond", id(cond)))
    blobs_at = pool_at + len(pool) * 8

    # Blobs, in source order so an untouched file's region comes back identical.
    # `conditions` and `args` are already deduplicated, so a shared record's
    # payload is written once, not once per reference.
    blob_records = [(c.blob_order, c.evaluator, ("cond", id(c))) for c in conditions
                    if c.evaluator] + \
                   [(a.blob_order, a.bytecode, ("arg", id(a))) for a in args
                    if a.bytecode]
    known = sorted((b for b in blob_records if b[0] is not None), key=lambda b: b[0])
    fresh = [b for b in blob_records if b[0] is None]
    blob_at, blob_bytes = {}, bytearray()
    for _order, payload, key in known + fresh:
        blob_at[key] = blobs_at + len(blob_bytes)
        blob_bytes += payload
    name_at = blobs_at + len(blob_bytes)
    name_len = len(archive.name) + 1 if archive.name else 0
    data_end = name_at + name_len * 2

    out = bytearray()
    out += struct.pack("<4sIIIII", MAGIC, 1, 3, 3, 0x54, data_end)
    out += struct.pack("<I", 6)
    for size, count in ((INTERNAL_HEADER_SIZE, 1), (GROUP_SIZE, group_count),
                        (STATE_SIZE, state_count), (CONDITION_SIZE, len(conditions)),
                        (COMMAND_CALL_SIZE, len(calls)), (COMMAND_ARG_SIZE, len(args))):
        out += struct.pack("<II", size, count)
    out += struct.pack("<IIII", pool_at, len(pool), name_at, name_len)
    out += struct.pack("<IIII", data_end, 0, data_end, 0)

    out += struct.pack("<I", 1) + struct.pack("<IIII", *archive.unk) + struct.pack("<I", 0)
    out += struct.pack("<qqqqqq", groups_at, group_count,
                       name_at if name_len else -1, name_len, -1, -1)

    for group in groups:
        first = state_slot[(group.id, entry[group.id].id)]
        at = states_at + first * STATE_SIZE
        out += struct.pack("<qqqq", group.id, at, len(group.states), at)

    def call_span(items, owner):
        # A run is addressed as (first slot, count) -- the reader recovers the
        # rest by walking forward from `first`, the same way `read_calls` does.
        # That only works if the run is genuinely contiguous in the rewritten
        # table. Every call here starts out that way (source order is
        # preserved, and a wholly-fresh owner's calls land together at the
        # tail), but a merge that appends one call to an *existing* owner's
        # list -- without relaying out the whole table -- breaks it: the new
        # call lands at the tail with the other fresh records, not next to
        # its owner. Silently trusting `first` then would hand back whatever
        # call happens to occupy the neighbouring slot (or, if the run runs
        # off the front of the table, blow up as a raw struct.error deep in a
        # read). Refuse instead of guessing.
        if not items:
            return -1, 0
        first = call_slot[id(items[0])]
        for i, item in enumerate(items):
            if call_slot[id(item)] != first + i:
                raise EsdError(
                    f"{owner} does not occupy a contiguous run in the "
                    f"rewritten call table -- inserting into an existing "
                    f"owner's list without relaying out the whole table is "
                    f"not supported")
        return calls_at + first * COMMAND_CALL_SIZE, len(items)

    for group_id, state in state_rows:
        # Unlike every other empty-list field in this format, a state's own
        # condition-pool reservation is unconditional: `reserve` above already
        # ran for every state, so a state with zero conditions still has a
        # `pool_start` entry -- it just points at wherever the cursor stood
        # at the time, never appended to. Real bossres/melina bytes confirm
        # this (co_off is never -1 for a zero-condition state, unlike vanilla,
        # which does use -1 there); using -1 instead breaks byte-exactness.
        co_off = pool_start[("state", group_id, state.id)]
        en_off, en_n = call_span(state.entry, f"state {group_id}/{state.id} entry")
        ex_off, ex_n = call_span(state.exit, f"state {group_id}/{state.id} exit")
        wh_off, wh_n = call_span(state.while_, f"state {group_id}/{state.id} while")
        out += struct.pack("<qqqqqqqqq", state.id, co_off, len(state.conditions),
                           en_off, en_n, ex_off, ex_n, wh_off, wh_n)

    for cond in conditions:
        target = -1
        if cond.target is not None:
            owner = cond_group[id(cond)]
            slot = state_slot.get((owner, cond.target))
            if slot is None:
                raise EsdError(
                    f"condition in group {owner} jumps to state {cond.target}, "
                    f"which that group does not have -- a merge left a jump "
                    f"target behind when it renumbered")
            target = states_at + slot * STATE_SIZE
        pc_off, pc_n = call_span(cond.pass_commands,
                                  f"condition (order={cond.order}) pass_commands")
        sc_off = pool_start.get(("cond", id(cond)), -1) if cond.subconditions else -1
        ev_off = blob_at.get(("cond", id(cond)), -1) if cond.evaluator else -1
        out += struct.pack("<qqqqqqq", target, pc_off, pc_n, sc_off,
                           len(cond.subconditions), ev_off, len(cond.evaluator))

    # A call's own arg-table reservation is unconditional too, the same way a
    # state's condition-pool reservation is: the args table is one flat run
    # per call, back to back in call order, so a zero-arg call still gets an
    # offset -- wherever the cursor stood -- rather than -1 (confirmed against
    # bossres/melina; vanilla is the one that uses -1 there). That means the
    # offset is a running cursor, not a lookup, so unlike `call_span` this
    # can't fall back on the args' own slot to find where a run starts -- it
    # has to check the args actually landed where the cursor says they did.
    # A merge that adds one arg to an existing call's list without relaying
    # out the whole table breaks that (the new arg lands at the tail with
    # the other fresh records, not next to its call) and must be refused
    # rather than silently pointing at whatever landed in the gap.
    args_cursor = 0
    for call in calls:
        for i, arg in enumerate(call.args):
            if arg_slot[id(arg)] != args_cursor + i:
                raise EsdError(
                    f"command call (bank={call.bank}, id={call.id}, "
                    f"order={call.order}) does not occupy a contiguous run "
                    f"in the rewritten arg table -- inserting into an "
                    f"existing call's arg list without relaying out the "
                    f"whole table is not supported")
        a_off = args_at + args_cursor * COMMAND_ARG_SIZE
        args_cursor += len(call.args)
        out += struct.pack("<iiqq", call.bank, call.id, a_off, len(call.args))
    for arg in args:
        b_off = blob_at.get(("arg", id(arg)), -1) if arg.bytecode else -1
        out += struct.pack("<qq", b_off, len(arg.bytecode))
    for slot_value in pool:
        out += struct.pack("<q", slot_value)
    out += blob_bytes
    if name_len:
        out += archive.name.encode("utf-16-le") + b"\0\0"
    return bytes(out)
