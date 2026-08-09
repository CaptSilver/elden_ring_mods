"""TPF: the texture container behind the game's menu icons.

Flat by construction — header, entry table, name block, then every texture's
bytes end to end in table order, no padding anywhere. That makes a from-scratch
writer safe here in a way it wouldn't be for BND4, which carries a hash-bucket
table only FromSoft's own hash function can reproduce (see bnd4.py). Everything
a TPF holds is either copied verbatim or recomputed from the textures.

Two mods that both ship menu/hi/01_common.tpf.dcx are usually not in conflict:
each is the whole ~204 MB vanilla atlas set with its own icons layered in, so
the merge is a union over texture names.
"""
import struct
from typing import NamedTuple

from ..errors import ErmError

MAGIC = b"TPF\0"
HEADER_SIZE = 0x10
ENTRY_SIZE = 0x14
# The name block is padded before the texture data starts. Measured, not
# assumed: Clever's block ends already aligned and carries no padding, the Great
# Rune Overhaul one ends at 0xa46 and carries exactly 2 bytes — which is the
# only alignment that reproduces both shipped files byte-for-byte. Cosmetic to
# the game either way, since every texture is addressed by an explicit offset.
NAME_ALIGN = 4


class TpfError(ErmError):
    """A TPF was malformed, or used a layout the writer can't express."""


class Texture(NamedTuple):
    name: str
    data: bytes
    format: int
    type: int
    mipmaps: int
    flags1: int


class Archive(NamedTuple):
    textures: tuple
    platform: int
    flag2: int
    encoding: int

    def with_textures(self, textures):
        return self._replace(textures=tuple(textures))


def _decode_name(data, offset, encoding):
    if offset >= len(data):
        raise TpfError(f"texture name offset {offset} is outside the file")
    if encoding == 1:
        # UTF-16LE, so the terminator is two zero bytes on an even boundary --
        # scanning for a single 0x00 stops on the high byte of the first ASCII
        # character and yields a one-letter name.
        end = offset
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        return data[offset:end].decode("utf-16-le", "replace")
    end = data.find(b"\0", offset)
    if end < 0:
        raise TpfError("unterminated texture name")
    return data[offset:end].decode("shift_jis", "replace")


def read(data):
    """Parse a TPF. Raises TpfError rather than returning a partial view."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise TpfError("not a TPF archive (bad magic)")
    _data_size, count, platform, flag2, encoding, _pad = struct.unpack_from(
        "<II BBBB", data, 4)
    # Untrusted third-party archives: reject a count the file can't hold before
    # the loop below reads past the end of the buffer.
    if HEADER_SIZE + count * ENTRY_SIZE > len(data):
        raise TpfError(
            f"TPF claims {count} textures, which doesn't fit in {len(data)} bytes")
    if platform != 0:
        raise TpfError(
            f"TPF platform {platform} isn't PC — its entry headers carry extra "
            f"fields this reader doesn't parse")

    textures = []
    for i in range(count):
        off = HEADER_SIZE + i * ENTRY_SIZE
        foff, fsize, fmt, ttype, mips, flags1 = struct.unpack_from("<IIBBBB", data, off)
        noff, has_float = struct.unpack_from("<II", data, off + 12)
        if has_float:
            raise TpfError(
                f"texture {i} carries a FloatStruct, which makes the entry stride "
                f"variable — refusing rather than misreading every entry after it")
        if foff + fsize > len(data):
            raise TpfError(
                f"texture {i} data runs past the end of the file "
                f"({foff}+{fsize} > {len(data)})")
        textures.append(Texture(_decode_name(data, noff, encoding),
                                data[foff:foff + fsize], fmt, ttype, mips, flags1))
    return Archive(tuple(textures), platform, flag2, encoding)


def write(archive):
    """Serialise an Archive back to TPF bytes.

    Offsets are recomputed from scratch, which is the whole point: appending a
    texture grows the entry table and the name block, so every data offset after
    it moves. Reproduces a shipped file byte-for-byte when nothing changed.
    """
    count = len(archive.textures)
    table_end = HEADER_SIZE + count * ENTRY_SIZE
    names, name_offsets, cursor = b"", [], table_end
    for tex in archive.textures:
        name_offsets.append(cursor)
        encoded = (tex.name.encode("utf-16-le") + b"\0\0" if archive.encoding == 1
                   else tex.name.encode("shift_jis") + b"\0")
        names += encoded
        cursor += len(encoded)

    pad = -len(names) % NAME_ALIGN
    table, body, data_at = b"", [], cursor + pad
    for tex, noff in zip(archive.textures, name_offsets):
        table += struct.pack("<IIBBBBII", data_at, len(tex.data), tex.format,
                             tex.type, tex.mipmaps, tex.flags1, noff, 0)
        body.append(tex.data)
        data_at += len(tex.data)

    payload = b"".join(body)
    # dataSize spans everything after the name block, so the alignment padding
    # counts towards it — that's what both shipped files record.
    header = struct.pack("<4sIIBBBB", MAGIC, pad + len(payload), count,
                         archive.platform, archive.flag2, archive.encoding, 0)
    return header + table + names + b"\0" * pad + payload
