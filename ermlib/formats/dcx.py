"""DCX container: the compression wrapper around every packed game archive.

Reading handles Oodle Kraken (via the vendored decompressor) and zlib DFLT.
Writing only emits DFLT, and that is deliberate: there is no open-source Kraken
encoder, and the game reads DFLT fine. Clever's Moveset ships 51 DFLT archives
alongside 7 KRAK ones and loads correctly, which is the local proof.
"""
import struct
import zlib

from ..errors import ErmError
from . import ooz

MAGIC = b"DCX\x00"
KRAK = b"KRAK"
DFLT = b"DFLT"
HEADER_SIZE = 0x4C
# The game validates this as 1-9 before dispatching to the zlib path.
MIN_LEVEL, MAX_LEVEL = 1, 9
MAX_UNK04 = 0x11000


class DcxError(ErmError):
    """A DCX container was malformed, or used a compression we can't read."""


def read(data):
    """Decompress a DCX container and return its payload."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise DcxError("not a DCX container (bad magic)")
    uncompressed, compressed = struct.unpack_from(">II", data, 0x1C)
    method = data[0x28:0x2C]
    body = data[HEADER_SIZE:HEADER_SIZE + compressed]
    if len(body) < compressed:
        raise DcxError(
            f"DCX is truncated: header claims {compressed} compressed bytes, "
            f"file holds {len(body)}")
    if method == KRAK:
        return ooz.decompress(body, uncompressed)
    if method == DFLT:
        out = zlib.decompress(body)
        if len(out) != uncompressed:
            raise DcxError(
                f"DFLT payload is {len(out)} bytes, header claims {uncompressed}")
        return out
    raise DcxError(f"unsupported DCX compression {method!r}")


def write_dflt(payload, level=9):
    """Wrap `payload` in a zlib-compressed DCX container.

    The layout mirrors the DFLT archives Clever's Moveset ships, which the game
    loads today — deviating from it is not worth the risk for a few KB.
    """
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise DcxError(
            f"zlib level {level} is outside 1-9; the game's DCX reader "
            f"rejects the container before decompressing it")
    body = zlib.compress(payload, level)
    header = bytearray()
    header += MAGIC + struct.pack(">IIIII", 0x11000, 0x18, 0x24, 0x44, 0x4C)
    header += b"DCS\x00" + struct.pack(">II", len(payload), len(body))
    header += b"DCP\x00" + DFLT + struct.pack(">I", 0x20)
    header += bytes([level, 0, 0, 0]) + struct.pack(">III", 0, 0, 0)
    header += struct.pack(">I", 0x00010100)
    header += b"DCA\x00" + struct.pack(">I", 8)
    assert len(header) == HEADER_SIZE, len(header)
    return bytes(header) + body
