"""DCX container: the compression wrapper around every packed game archive.

Reading handles Oodle Kraken (via the vendored decompressor), zlib DFLT, and
Zstandard. Writing emits DFLT or ZSTD but never Kraken — there is no open-source
Kraken encoder, and the game reads the other two fine. Clever's Moveset ships 51
DFLT archives alongside 7 KRAK ones and loads correctly, which is the local
proof for DFLT; regulation.bin is itself ZSTD, which is the proof for ZSTD.

Pick the writer to match what the file already was. "Kraken in, zlib out" is
established for msgbnd, but a regulation.bin arrives as ZSTD and should leave as
ZSTD: nothing has ever demonstrated the game accepting a DFLT regulation on a
current build.
"""
import importlib
import platform
import struct
import zlib

from ..errors import ErmError
from . import ooz

MAGIC = b"DCX\x00"
KRAK = b"KRAK"
DFLT = b"DFLT"
ZSTD = b"ZSTD"
HEADER_SIZE = 0x4C
# The game validates this as 1-9 before dispatching to the *zlib* branch. It is
# not a general DCX rule: shipped ZSTD regulations carry 15 (Clever's) and 21
# (vanilla) in the same byte.
MIN_LEVEL, MAX_LEVEL = 1, 9
# Matches Clever's regulation.bin, which the game loads today.
ZSTD_LEVEL = 15
# Cap the decoder's window requirement at 64 KiB. Both mod-authored regulations
# on this machine declare exactly this (descriptor byte 0x30) and both load; a
# frame asking for 4 MiB crashed the game a few seconds into startup, with byte-
# identical content to one that worked. Vanilla's own regulation asks for 64 MiB
# and is fine, but it is the one file never served through the loader's file
# override -- so it is not evidence about this path.
ZSTD_WINDOW_LOG = 16
MAX_UNK04 = 0x11000


class DcxError(ErmError):
    """A DCX container was malformed, or used a compression we can't read."""


def _zstd():
    """The stdlib zstd module, or a DcxError explaining why it isn't here.

    Resolved on use, never at import. `compression.zstd` arrived in Python 3.14,
    and SteamOS ships older — importing it at module scope took all of erm down
    on the Deck, including every command that has nothing to do with ZSTD.
    regulation.bin is the only ZSTD file in play, so everything else keeps
    working on an older interpreter.
    """
    try:
        return importlib.import_module("compression.zstd")
    except ImportError as exc:
        raise DcxError(
            f"ZSTD DCX support needs Python 3.14's compression.zstd, and this "
            f"interpreter is {platform.python_version()}. Only regulation.bin "
            f"uses ZSTD — the rest of erm works without it, so this is a merge "
            f"you can't run here rather than an install you can't do."
        ) from exc


def read(data):
    """Decompress a DCX container and return its payload."""
    if data[:4] != MAGIC:
        raise DcxError("not a DCX container (bad magic)")
    if len(data) < HEADER_SIZE:
        raise DcxError(
            f"DCX header is truncated: need {HEADER_SIZE} bytes, file holds {len(data)}")
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
    if method == ZSTD:
        zstd = _zstd()
        try:
            out = zstd.decompress(body)
        except zstd.ZstdError as exc:
            # Keep the module's error contract: callers catch DcxError, and a
            # corrupt body is a malformed container, not a programming fault.
            raise DcxError(f"ZSTD body could not be decompressed: {exc}") from exc
        if len(out) != uncompressed:
            raise DcxError(
                f"ZSTD payload is {len(out)} bytes, header claims {uncompressed}")
        return out
    raise DcxError(f"unsupported DCX compression {method!r}")


def _header(method, level, uncompressed, compressed):
    """The 0x4C DCX header. Every field outside `method` and `level` is fixed,
    and verified byte-for-byte against both a shipped DFLT archive and Clever's
    ZSTD regulation.bin — the two containers differ only in those two places."""
    header = bytearray()
    header += MAGIC + struct.pack(">IIIII", MAX_UNK04, 0x18, 0x24, 0x44, HEADER_SIZE)
    header += b"DCS\x00" + struct.pack(">II", uncompressed, compressed)
    header += b"DCP\x00" + method + struct.pack(">I", 0x20)
    header += bytes([level, 0, 0, 0]) + struct.pack(">III", 0, 0, 0)
    header += struct.pack(">I", 0x00010100)  # fixed value; confirmed against real game archives
    header += b"DCA\x00" + struct.pack(">I", 8)
    assert len(header) == HEADER_SIZE, len(header)
    return bytes(header)


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
    return _header(DFLT, level, len(payload), len(body)) + body


def write_zstd(payload, level=ZSTD_LEVEL):
    """Wrap `payload` in a Zstandard DCX container, the way regulation.bin ships.

    Deliberately the STREAMING compressor. The one-shot `zstd.compress` sets the
    frame-header content-size flag (FHD bit 7); every shipped file has FHD 0x00,
    so the one-shot output is structurally unlike anything the game has loaded.
    The bytes still decompress either way, which is exactly why this is worth
    pinning down in code rather than leaving to whoever edits it next.
    """
    zstd = _zstd()
    compressor = zstd.ZstdCompressor(options={
        zstd.CompressionParameter.compression_level: level,
        # Not a compression-ratio knob: it is what the DECODER is required to
        # allocate, and something on the override path refuses a large one.
        zstd.CompressionParameter.window_log: ZSTD_WINDOW_LOG,
    })
    body = compressor.compress(payload) + compressor.flush()
    return _header(ZSTD, level, len(payload), len(body)) + body
