"""BND4: the archive container holding the FMGs inside a msgbnd.

Rebuilding is a structural clone rather than a fresh serialization. We only ever
change entry *contents*, never which entries exist, so everything from the name
blob to the start of the data section is copied verbatim -- including the
hash-bucket table at 0x38, which vanilla populates and which a from-scratch
writer would have to reimplement FromSoft's hash algorithm to reproduce.
"""
import struct
from typing import NamedTuple

from ..errors import ErmError

MAGIC = b"BND4"
HEADER_SIZE = 0x40
DATA_ALIGN = 0x10
# Fixed fields read out of every entry header: size, size, offset, id, name_offset.
ENTRY_FIELDS_SIZE = 0x24


class Bnd4Error(ErmError):
    """A BND4 archive was malformed, or a rebuild was asked for something
    the structural clone can't express."""


class Entry(NamedTuple):
    id: int
    name: str
    data: bytes


def _entry_layout(data):
    """(count, entry_header_size, data_offset) from the BND4 header.

    These files are arbitrary third-party mod archives, so every header field
    is treated as untrusted: a bad value must raise Bnd4Error here, before any
    loop below has a chance to read past the buffer or hang on a bogus count.
    """
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise Bnd4Error("not a BND4 archive (bad magic)")
    count, = struct.unpack_from("<i", data, 0x0C)
    entry_header_size, = struct.unpack_from("<q", data, 0x20)
    data_offset, = struct.unpack_from("<q", data, 0x28)

    if count < 0:
        raise Bnd4Error(f"BND4 entry count is negative: {count}")
    # entry_header_size smaller than the fields every entry holds (or <= 0)
    # with entries present either shrinks headers_end below HEADER_SIZE
    # (caught below), lets a too-small size overlap adjacent entries' fields,
    # or -- if exactly 0 -- lets an absurd count sail through the
    # table-fits-the-file check untouched and hang read()'s `range(count)`
    # loop instead of raising. Requiring the full field size up front also
    # means read()'s per-entry loop never has to re-check this bound itself.
    if count > 0 and entry_header_size < ENTRY_FIELDS_SIZE:
        raise Bnd4Error(
            f"BND4 entry header size {entry_header_size} is smaller than the "
            f"{ENTRY_FIELDS_SIZE} bytes of fields every entry header must hold")

    headers_end = HEADER_SIZE + count * entry_header_size
    if headers_end < HEADER_SIZE or headers_end > len(data):
        raise Bnd4Error(
            f"BND4 entry header table (count={count}, header_size={entry_header_size}) "
            f"doesn't fit the {len(data)}-byte buffer"
        )
    if data_offset < headers_end or data_offset > len(data):
        raise Bnd4Error(
            f"BND4 data section (offset {data_offset}) overlaps the entry headers "
            f"or exceeds the {len(data)}-byte buffer"
        )
    return count, entry_header_size, data_offset


def _read_utf16z(data, offset):
    end = offset
    while data[end:end + 2] != b"\x00\x00":
        if end >= len(data):
            raise Bnd4Error("unterminated entry name in BND4")
        end += 2
    return data[offset:end].decode("utf-16-le")


def read(data):
    """Parse a BND4 into its entries, in file order."""
    count, entry_header_size, data_offset = _entry_layout(data)
    entries = []
    spans = []
    for i in range(count):
        base = HEADER_SIZE + i * entry_header_size
        # _entry_layout already guarantees entry_header_size >= ENTRY_FIELDS_SIZE
        # and that the whole header table fits the file, so base + ENTRY_FIELDS_SIZE
        # is always within bounds here.
        size, = struct.unpack_from("<q", data, base + 0x10)
        offset, = struct.unpack_from("<I", data, base + 0x18)
        entry_id, = struct.unpack_from("<i", data, base + 0x1C)
        name_offset, = struct.unpack_from("<i", data, base + 0x20)

        if size < 0:
            raise Bnd4Error(f"BND4 entry {i} (id={entry_id}) has a negative size: {size}")
        # A corrupted offset that lands before data_offset (0 is the extreme
        # case) slices into the header/name/hash region instead of the data
        # section, silently handing back the magic bytes or some other
        # unrelated field as this entry's "data".
        if offset < data_offset:
            raise Bnd4Error(
                f"BND4 entry {i} (id={entry_id}) data offset {offset} "
                f"precedes the declared data section at {data_offset}"
            )
        if offset + size > len(data):
            raise Bnd4Error(
                f"BND4 entry {i} (id={entry_id}) data (offset {offset}, size {size}) "
                f"runs past the end of the {len(data)}-byte buffer"
            )
        if name_offset < 0 or name_offset >= len(data):
            raise Bnd4Error(
                f"BND4 entry {i} (id={entry_id}) name offset {name_offset} "
                f"is outside the {len(data)}-byte buffer"
            )

        entries.append(Entry(entry_id, _read_utf16z(data, name_offset),
                             bytes(data[offset:offset + size])))
        spans.append((offset, size, i, entry_id))

    # Nothing above stops one entry's declared data region from overlapping
    # another's. An overlap means at least one of them isn't returning the
    # bytes its own header claims -- blended with its neighbor's -- so this
    # has to raise rather than hand back a value. Real archives lay entries
    # out back-to-back with only alignment padding between them, never
    # overlapping, so a strict non-overlap check doesn't cost us anything
    # real files rely on.
    sorted_spans = sorted(spans)
    for (offset, size, i, entry_id), (next_offset, _next_size, next_i, next_id) in zip(
            sorted_spans, sorted_spans[1:]):
        if next_offset < offset + size:
            raise Bnd4Error(
                f"BND4 entry {i} (id={entry_id}) data region [{offset}, {offset + size}) "
                f"overlaps entry {next_i} (id={next_id}) starting at {next_offset}"
            )
    return entries


def rebuild(base, replacements):
    """Return `base` with the data of the given entry ids replaced.

    Only entry contents change. The header, entry names, and hash table are
    carried across untouched, so offsets into them stay valid.
    """
    count, entry_header_size, data_offset = _entry_layout(base)
    entries = read(base)
    known = {e.id for e in entries}
    unknown = set(replacements) - known
    if unknown:
        raise Bnd4Error(
            f"BND4 has no entry with id {sorted(unknown)} — the archive layout "
            f"changed, so this merge would silently do nothing")

    if not replacements:
        # Nothing to change, so the input already is the answer. Real
        # archives' data_offset isn't always itself aligned to where the
        # first entry actually starts (vanilla can leave a few pad bytes
        # between the hash table and the data section) -- recomputing this
        # case from scratch would risk drifting from the input instead of
        # reproducing it, for a rebuild that has nothing to do anyway.
        return base

    # unknown was empty and replacements is nonempty, so at least one entry
    # exists: count > 0 is guaranteed here.
    out = bytearray(base[:data_offset])
    blob = bytearray()
    # The header's data_offset marks the end of the hash table, not
    # necessarily an aligned entry start -- real archives can leave a few
    # padding bytes between the two. Anchor on the first entry's own recorded
    # offset and carry that gap across verbatim rather than assuming it's zero.
    first_offset, = struct.unpack_from("<I", base, HEADER_SIZE + 0x18)
    if first_offset < data_offset:
        raise Bnd4Error(
            f"BND4 first entry offset {first_offset} precedes the "
            f"declared data section at {data_offset}")
    out += base[data_offset:first_offset]
    cursor = first_offset
    for i, entry in enumerate(entries):
        payload = replacements.get(entry.id, entry.data)
        header = HEADER_SIZE + i * entry_header_size
        struct.pack_into("<q", out, header + 0x08, len(payload))
        struct.pack_into("<q", out, header + 0x10, len(payload))
        struct.pack_into("<I", out, header + 0x18, cursor)
        blob += payload
        cursor += len(payload)
        # Padding aligns the *next* entry's start to 0x10; there is no next
        # entry after the last one, and real archives carry no trailing
        # padding -- adding it here would silently grow the file past its
        # original length.
        if i < count - 1:
            pad = (-len(payload)) % DATA_ALIGN
            blob += b"\x00" * pad
            cursor += pad
    return bytes(out + blob)
