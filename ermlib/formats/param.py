"""PARAM: one game-data table, as found inside regulation.bin's BND4.

Rows are treated as opaque fixed-width blobs keyed by id. That is deliberate and
it is what makes a narrow patch possible without shipping paramdefs: transplanting
a row between two files needs the row's bytes and its stride, nothing more. The
moment you want to read or edit a *field* you need a paramdef, and that is a much
larger commitment — see the row-blob decision recorded for the NoFallDead patch.

Layout (little-endian), verified against Elden Ring's SpEffectParam:

    0x00  u32  strings_off
    0x04  u16  short_data_off        (0 on this format)
    0x06  u16  unk06
    0x08  u16  paramdef_data_version
    0x0A  u16  row_count
    0x10  i64  param_type_off
    0x2C  4B   format flags
    0x30  i64  data_off
    0x40       row table, 24 bytes each: i32 id, u32 pad, i64 data_off, i64 name_off
               row data, `row_count * stride` bytes
               strings block, to end of file

Every row in SpEffectParam shares a single `name_off` pointing at a run of zero
bytes — row names are an editor convenience the game never stores — so writing
one back is a matter of shifting offsets, not building a string table.
"""
import struct
from typing import NamedTuple

from ..errors import ErmError

HEADER_SIZE = 0x40
ROW_ENTRY = 24


class ParamError(ErmError):
    """A PARAM was malformed, or an edit would produce one that is."""


class Row(NamedTuple):
    id: int
    data: bytes
    name_off: int      # absolute offset into the source file's strings block


class Param(NamedTuple):
    rows: tuple
    stride: int
    header: bytes       # the original 0x40 header, re-stamped on write
    strings: bytes      # the strings block, copied verbatim
    strings_off: int    # where `strings` sat in the source, to compute the shift
    param_type_off: int
    pad: bytes = b""    # alignment bytes between the last row and the strings

    @property
    def param_type(self):
        """The param's type name, e.g. SP_EFFECT_PARAM_ST, or b"" if it is
        stored outside the strings block (some params keep it earlier in the
        file, which this reader preserves but does not decode)."""
        rel = self.param_type_off - self.strings_off
        if rel < 0 or rel >= len(self.strings):
            return b""
        end = self.strings.find(b"\x00", rel)
        return self.strings[rel:end if end != -1 else None]

    def row_ids(self):
        return [r.id for r in self.rows]


def _derive_stride(offsets, strings_off, row_count):
    """Row width, from the gaps between consecutive row-data offsets.

    Deliberately NOT (strings_off - data_off) / row_count: that formula silently
    absorbs any trailing alignment padding into the stride, which then slices
    every row a few bytes wide. With one row there are no gaps to measure, so
    that formula is the only option left and the padding risk is unavoidable.
    """
    if row_count == 1:
        return strings_off - offsets[0]
    deltas = {b - a for a, b in zip(offsets, offsets[1:])}
    if len(deltas) != 1:
        raise ParamError(
            f"row data is not fixed-stride: consecutive offsets differ by "
            f"{sorted(deltas)[:5]} — this param can't be row-patched safely")
    return deltas.pop()


def read(data):
    """Parse a PARAM. Raises ParamError rather than returning a partial view."""
    if len(data) < HEADER_SIZE:
        raise ParamError(f"PARAM header is truncated: {len(data)} bytes")
    strings_off, = struct.unpack_from("<I", data, 0x00)
    row_count, = struct.unpack_from("<H", data, 0x0A)
    param_type_off, = struct.unpack_from("<q", data, 0x10)
    data_off, = struct.unpack_from("<q", data, 0x30)

    table_end = HEADER_SIZE + row_count * ROW_ENTRY
    if row_count and data_off != table_end:
        raise ParamError(
            f"row data starts at {data_off}, but the {row_count}-entry row table "
            f"ends at {table_end} — unexpected gap")
    if not (0 < strings_off <= len(data)) or table_end > len(data):
        raise ParamError(
            f"PARAM offsets fall outside the file (strings_off={strings_off}, "
            f"row table end={table_end}, size={len(data)})")

    entries = [struct.unpack_from("<iIqq", data, HEADER_SIZE + i * ROW_ENTRY)
               for i in range(row_count)]
    offsets = [e[2] for e in entries]
    if offsets != sorted(offsets):
        raise ParamError("row data offsets are not ascending in row-table order")
    stride = _derive_stride(offsets, strings_off, row_count) if row_count else 0

    rows = []
    for rid, _pad, off, name_off in entries:
        # name_off 0 is a real convention meaning "no name", and plenty of
        # shipped params use it. Anything else has to land inside the file.
        if name_off and not 0 <= name_off <= len(data):
            raise ParamError(
                f"row {rid} has a name offset ({name_off}) outside the file")
        if off + stride > strings_off:
            raise ParamError(f"row {rid} data overruns the strings block")
        rows.append(Row(rid, data[off:off + stride], name_off))

    if not 0 <= param_type_off <= len(data):
        raise ParamError(f"param type offset {param_type_off} is outside the file")
    # Some params align the strings block, leaving a gap after the last row.
    # Recomputing strings_off as data_off + n*stride would silently close it and
    # shift every string offset, so carry the gap through verbatim.
    return Param(tuple(rows), stride, data[:HEADER_SIZE], data[strings_off:],
                 strings_off, param_type_off,
                 data[data_off + row_count * stride:strings_off])


def replace_rows(param, rows):
    """Return `param` with a new row list, ordered by id.

    Ordering here rather than at write time keeps the invariant in one place: the
    game looks rows up by id over the table, so an out-of-order table makes rows
    unreachable without corrupting anything a parser would notice. `write` itself
    preserves whatever order it is given, so reading and writing a param that
    ships out of order (PhantomParam has 910 before 800) leaves it untouched.
    """
    return param._replace(rows=tuple(sorted(rows, key=lambda r: r.id)))


def patch_rows(param, overwrite=None, insert=None):
    """Return `param` with rows overwritten and/or inserted, ids kept ascending.

    The two arguments are separate on purpose. A transplant knows which rows it
    expects to already exist and which it expects to be new, and getting that
    backwards is precisely how a patch silently destroys an authored row or
    silently duplicates one. Stating the intent lets both mistakes be errors.

    Duplicate ids are legal in shipped params (RandomAppearParam has 5,322 rows
    for 5,296 unique ids), so `write` tolerates them — but patching into one is
    ambiguous, so it is refused here rather than guessing which copy to touch.
    """
    overwrite = dict(overwrite or {})
    insert = dict(insert or {})
    existing = param.row_ids()
    if len(set(existing)) != len(existing):
        dupes = sorted({i for i in existing if existing.count(i) > 1})
        raise ParamError(
            f"{param.param_type.decode() or 'param'} has duplicate row ids "
            f"{dupes[:5]} — refusing to patch a param where an id is ambiguous")
    if existing != sorted(existing):
        raise ParamError("row ids are not ascending — refusing to patch")
    by_id = {r.id: r for r in param.rows}

    missing = sorted(set(overwrite) - by_id.keys())
    if missing:
        raise ParamError(
            f"expected to overwrite rows {missing}, but they are not in this param "
            f"— the base file is not what the patch was built against")
    clashing = sorted(set(insert) & by_id.keys())
    if clashing:
        raise ParamError(
            f"expected to insert rows {clashing}, but they already exist — the base "
            f"file already carries them, so inserting would duplicate an id")

    name_off = param.rows[0].name_off if param.rows else 0
    rows = [r._replace(data=overwrite[r.id]) if r.id in overwrite else r
            for r in param.rows]
    rows += [Row(rid, blob, name_off) for rid, blob in insert.items()]
    return replace_rows(param, rows)


def write(param):
    """Serialise a PARAM back to bytes, preserving row order as given."""
    n = len(param.rows)
    widths = {len(r.data) for r in param.rows}
    if len(widths) > 1 or (widths and widths.pop() != param.stride):
        raise ParamError(
            f"every row must be exactly the param's stride ({param.stride} bytes); "
            f"got widths {sorted({len(r.data) for r in param.rows})}")

    data_off = HEADER_SIZE + n * ROW_ENTRY
    strings_off = data_off + n * param.stride + len(param.pad)
    # All string offsets are absolute, so moving the block moves them together.
    shift = strings_off - param.strings_off
    if shift:
        # Only offsets inside the strings block move with it. An offset pointing
        # anywhere earlier belongs to a region whose displacement we haven't
        # measured, so relocating it would be a guess.
        stray = [r.id for r in param.rows if r.name_off and r.name_off < param.strings_off]
        if stray:
            raise ParamError(
                f"rows {stray[:5]} name offsets point before the strings block, so "
                f"they can't be relocated when the row count changes")
        if param.param_type_off < param.strings_off:
            raise ParamError(
                "param type string sits before the strings block, so it can't be "
                "relocated when the row count changes")

    header = bytearray(param.header)
    struct.pack_into("<I", header, 0x00, strings_off)
    struct.pack_into("<H", header, 0x0A, n)
    struct.pack_into("<q", header, 0x10, param.param_type_off + shift)
    struct.pack_into("<q", header, 0x30, data_off)

    table = bytearray()
    for i, row in enumerate(param.rows):
        # 0 is the "no name" sentinel, not an address — shifting it would turn
        # it into a pointer at whatever now sits at `shift`.
        table += struct.pack("<iIqq", row.id, 0, data_off + i * param.stride,
                             row.name_off + shift if row.name_off else 0)
    body = b"".join(r.data for r in param.rows)
    return bytes(header) + bytes(table) + body + param.pad + param.strings
