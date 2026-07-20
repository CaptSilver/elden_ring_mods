"""FMG: the message table inside a msgbnd. Maps a numeric text id to a string.

Elden Ring uses FMG version 2 (64-bit string offsets). Ids are stored as
consecutive ranges plus a flat offset array, so a file with sparse ids costs one
range per run rather than one per id.
"""
import struct

from ..errors import ErmError

VERSION = 2
HEADER_SIZE = 0x28
RANGE_SIZE = 0x10


class FmgError(ErmError):
    """An FMG was malformed or used an unsupported version."""


def _read_utf16z(data, offset):
    end = offset
    while data[end:end + 2] != b"\x00\x00":
        if end >= len(data):
            raise FmgError("unterminated string in FMG")
        end += 2
    return data[offset:end].decode("utf-16-le")


def read(data):
    """Parse an FMG into {id: str or None}. None means the id exists with no text."""
    if len(data) < HEADER_SIZE:
        raise FmgError("FMG is shorter than its header")
    version = data[2]
    if version != VERSION:
        raise FmgError(f"unsupported FMG version {version}, expected {VERSION}")
    range_count, string_count = struct.unpack_from("<ii", data, 0x0C)
    offsets_off, = struct.unpack_from("<q", data, 0x18)
    entries = {}
    for r in range(range_count):
        index, first, last = struct.unpack_from("<iii", data, HEADER_SIZE + r * RANGE_SIZE)
        for step, text_id in enumerate(range(first, last + 1)):
            slot = index + step
            if slot >= string_count:
                raise FmgError(f"FMG range {r} points past the offset table")
            string_off, = struct.unpack_from("<q", data, offsets_off + slot * 8)
            entries[text_id] = _read_utf16z(data, string_off) if string_off else None
    return entries


def _consecutive_ranges(ids):
    """Collapse a sorted id list into (first, last) runs."""
    ranges, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        ranges.append((ids[i], ids[j]))
        i = j + 1
    return ranges


def write(entries):
    """Serialize {id: str or None} to FMG version 2."""
    ids = sorted(entries)
    ranges = _consecutive_ranges(ids)
    offsets_off = HEADER_SIZE + len(ranges) * RANGE_SIZE
    strings_off = offsets_off + len(ids) * 8

    blob, offsets, shared = bytearray(), [], {}
    for text_id in ids:
        text = entries[text_id]
        if text is None:
            offsets.append(0)
            continue
        if text in shared:                     # vanilla shares repeated strings
            offsets.append(shared[text])
            continue
        position = strings_off + len(blob)
        shared[text] = position
        offsets.append(position)
        blob += text.encode("utf-16-le") + b"\x00\x00"

    total = strings_off + len(blob)
    out = bytearray(total)
    struct.pack_into("<BBBB", out, 0, 0, 0, VERSION, 0)
    struct.pack_into("<i", out, 4, total)
    struct.pack_into("<i", out, 8, 1)
    struct.pack_into("<i", out, 0x0C, len(ranges))
    struct.pack_into("<i", out, 0x10, len(ids))
    struct.pack_into("<i", out, 0x14, 0xFF)
    struct.pack_into("<q", out, 0x18, offsets_off)
    struct.pack_into("<q", out, 0x20, 0)

    slot = 0
    for r, (first, last) in enumerate(ranges):
        struct.pack_into("<iiii", out, HEADER_SIZE + r * RANGE_SIZE, slot, first, last, 0)
        slot += last - first + 1
    for i, offset in enumerate(offsets):
        struct.pack_into("<q", out, offsets_off + i * 8, offset)
    out[strings_off:strings_off + len(blob)] = blob
    return bytes(out)
