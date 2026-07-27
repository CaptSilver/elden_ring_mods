"""regulation.bin: the encrypted, compressed archive holding every param table.

Three layers, outermost first:

    AES-256-CBC          IV in the first 16 bytes, ciphertext after it
    DCX (ZSTD)           the compression wrapper
    BND4                 194 entries, one .param each

Padding convention, measured from real files rather than assumed. FromSoft's own
encryptor zero-pads the DCX to a 16-byte boundary and stops; SoulsFormats-derived
tools zero-pad and then append a full PKCS#7 block. We emit the latter, because
that is what Clever's Moveset ships and the game loads it today.
"""
from . import aes, bnd4, dcx

# Static across every retail build; the file is "encrypted" only in the sense
# that the key ships with the game.
KEY = bytes.fromhex(
    "99BFFC366A6BC8C6F5827D093602D676C42892A01C207FB024D3AF4E493FEF99")
IV_SIZE = 16
_PAD_BLOCK = 16


def unpack(blob):
    """Return the BND4 payload inside `blob`."""
    return dcx.read(aes.decrypt_cbc(KEY, blob[:IV_SIZE], blob[IV_SIZE:]))


def entries(blob):
    """Return the BND4 entries inside `blob`."""
    return bnd4.read(unpack(blob))


def pack(payload, iv, level=dcx.ZSTD_LEVEL):
    """Compress, pad and encrypt `payload` back into a regulation.bin.

    `iv` comes from the file being replaced rather than being generated: the
    output should differ from its input only where the content does, and CBC
    does not need a fresh IV for a file whose key is public anyway.
    """
    body = dcx.write_zstd(payload, level)
    padded = body + b"\x00" * (-len(body) % _PAD_BLOCK) + bytes([_PAD_BLOCK]) * _PAD_BLOCK
    return bytes(iv) + aes.encrypt_cbc(KEY, iv, padded)


def repack(blob, replacements):
    """Return `blob` with BND4 entries replaced, everything else preserved."""
    payload = bnd4.rebuild(unpack(blob), replacements)
    return pack(payload, blob[:IV_SIZE])
