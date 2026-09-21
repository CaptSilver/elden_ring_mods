"""Read one file out of Elden Ring's packed `Data*.bdt` archives.

The game ships almost nothing loose: `msg/`, `param/` and the rest live inside
five encrypted archives beside the executable. A mod that wants to change one
string therefore ships a whole copy of the game's file, and erm has to decide
whether to mount it or merge it. Without a reader, the only baselines available
were snapshots bundled by other mods, which lag the game by a patch or more --
and a merge base that is a patch behind silently reverts whatever the patch
added. This module supplies the game's own current copy instead.

Four layers, outermost first:

    RSA           the .bhd index, one 2048-bit public key per archive
    BHD5          buckets of 40-byte entries, keyed by a hash of the path
    AES-128-ECB   each payload in the .bdt, key and byte ranges in the index
    DCX           the ordinary compression wrapper `dcx.py` already reads

The .bdt files run to 20 GB, so nothing here reads one whole: a lookup seeks to
the entry's offset and reads exactly its padded size.
"""
import struct
from collections import namedtuple

from ..errors import ErmError
from . import aes

BLOCK = 16
_RSA_BLOCK = 256        # 2048-bit modulus
_ENTRY = struct.Struct("<QiiqQQ")

# Search order. Data0 holds the base game's msg/, DLC the Shadow of the Erdtree
# assets; a lookup walks them in this order and stops at the first hit.
ARCHIVES = ("Data0", "Data1", "Data2", "Data3", "DLC")

# One public key per archive, as (modulus, exponent). FromSoft encrypts the
# index with the *private* key and the game decrypts with the public one, so
# these are enough to read -- and useless for writing anything the game accepts.
# DLC.bhd reuses the key BinderTool calls the sd.bhd key; that is verified
# against the shipped file, not assumed.
RSA_KEYS = {
    "Data0": (int(
        "f518eedb086bb970d5419a5fca55443de3719bb5e0307703c9a691508a57404d"
        "2a128fba637e8b3f4c6916f2f41f490ac47c29b884ac760ae730727fca3e12eb"
        "5316ce13bccb80506f0ff325e8610d244279c149985073dc8d890b91428e82be"
        "f36c5df713395d775fb1d0734624a086e2072f8aa4d9667fd1ffe72b957281e5"
        "979ffa0b79804b8d7d52f30492012cdaeb82578edd1b3ff8ba9a957648b66a29"
        "1c68ef1d9b210ad1d721cd7a4485da306164881c6ed301c630b2c27fbba7de78"
        "c2371f8f1079556699e2119b2a18b56e160f71c86de179c685c51358edcd9895"
        "ac97071c8d0e24b2eb4bab7b918cb30cd087939103ba3eff3f47b50d16d7425b", 16), 0x4731fc21),
    "Data1": (int(
        "c5a0421d026bb4b26224d746f67ab775e03db18e18099e1d6d33873bebfe6205"
        "9131c13a88aea864826afa704c50d05b18f99b46b1239241fd33b2c0ca900538"
        "f282eb0307cea20c06c50cb987dfba506e3aab44043d75080c1fc77eeb8f027e"
        "ff5b6712ab498d892b2bdcb2ac837a63e8f2e1fb72990b6393cdfe2fcf69bcc3"
        "2690d79968f38de0ef6ae4cf4f9c5a0abcaf1e638e5957f27293e3defb173af9"
        "ee21e1cc7c30be54da430cdd50c026eb21e33ff4e614b7fdb0b807a2bdfbb74f"
        "c20a9980ac7527b0c1b4b74748083044fa628cf78e8ca7006b604368da7d09fa"
        "27ef4a02f99081f87910ec58c9705eca687b07e681efb88ec46409765d3b0b11", 16), 0x35aff625),
    "Data2": (int(
        "d220d5550db7d1182b9081c934383113b23fd80687e8b8b512ef66b697ebadf8"
        "5fa0ad9eef2e0e594fa58fb0062381888245a93f0f091da57def40abebeb131b"
        "84ebe5e3103f1ac2207ec12403c2968ca9c5cb3604d7d3197ce6eaf7947eb1b3"
        "2c80e9ef0cb6e25200f2bf59c14be92a5376f24c410c67826c60a461e49536e1"
        "7e47dacde0e00c458874af543de74c5cdaa48d96836c8e90e848aa183744fbbb"
        "9ae7327ff89ad7259d63d545653de34cebd6bca6bf825cdee98d7fa713d57aab"
        "4b581268ad495997ec49e0fa0f0937bafaaebdf63a625191ca0225544300bcaa"
        "c344c1f296c2edaaa85891935731dcb4357a297f5ac41cacbda6868712626577", 16), 0xffffffff),
    "Data3": (int(
        "bd144d06756add692708d1eb2517a5704036bff3b32a5424c59c35c8a965d18d"
        "8a9fa1bdb6cf7849f81961b7450a7217cb9344bb21d12abc57cf9beeae386b72"
        "ea18008d995215cf5d925d807cb069cfe2dd2fc863457f7836ea7a9c89587a4f"
        "e88e7ae724aa968975a7f0527158db12c572bd91ad5885cb3c36f1e04605280e"
        "41e76b8ae86773e2a732545b5512136832f83e1285a7c8b0fb114cee0762a197"
        "8e82b8d89cb725ab2e5207829289af5403f1ad257743c33c70d99f4deae900ef"
        "65ddb7516d9df49fbf1de06de768f4f3eaa80573f0867759b263cc5a78100da8"
        "a4e8533b1154507ad28f8ba879ba9ad598c012e7047f8d02154cca538ffcc977", 16), 0x5eccde43),
    # DLC.bhd reuses what BinderTool calls the sd.bhd key. Verified against
    # the shipped file rather than assumed -- it is the only archive whose
    # key is shared, and the only one BinderTool predates.
    "DLC": (int(
        "99827fe46254e1ba094af67cd4114e1d84c674158f1e7598972df25a8d350588"
        "c6467cfc3539330c7531b1b8c882d1b95eab107d9b2d6482d0f7b860488e4068"
        "0a021ab4fcb27d0f7978368b498ac0bdb58118d151dbcf0e9d58fbb860ac6c2e"
        "4e48f272aaac43fa882e6e0dbc91ef19177bc4b389f6bb36cf062171e455aeaf"
        "575360176dd638a5d090dfe1eea1a162274756d25cb9a9d1178db4afd901187b"
        "7ea7582b6c77a84463d2e36c15fc0f57cadc3d9ce82d2035d1c9187da210e354"
        "37a0cc2bdb74bf62419f008e650ed6f86b7b4a934dae4c7cc4a10cf201fd9dad"
        "7483d9ded753223b51672e6f22e333b155d3c74012880a429c192d68dc2d9d7f", 16), 0x5f0fdfdf),
}


class Bhd5Error(ErmError):
    """An archive index could not be read, or a path isn't in it."""


Entry = namedtuple("Entry", "name_hash padded_size unpadded_size offset key ranges")
Index = namedtuple("Index", "salt bucket_count entries")


def normalise(path):
    """The exact spelling the index hashes: lowercase, forward slashes, and a
    forced leading slash.

    The leading slash is the one that bites. Without it every lookup misses and
    reports "not in the archive", which reads like the file genuinely isn't
    there rather than like a normalisation bug.
    """
    text = str(path).strip().replace("\\", "/").lower()
    return text if text.startswith("/") else "/" + text


def path_hash(path):
    """FromSoft's index hash: `h = char + 133*h`, accumulated over 64 bits.

    Older games truncate this to 32 bits. Elden Ring does not -- a 32-bit
    accumulator finds nothing at all, which is the first thing to re-check if a
    future patch appears to empty the index.
    """
    h = 0
    for ch in str(path):
        h = (ord(ch) + 133 * h) & 0xFFFFFFFFFFFFFFFF
    return h


def decrypt_header(blob, archive):
    """RSA-decrypt a `.bhd`, returning the BHD5 plaintext.

    Each 256-byte ciphertext block becomes **255** bytes of plaintext, not 256.
    That asymmetry is the whole trick: the encryptor treated the input as
    255-byte blocks and the modexp result is left-padded back to the modulus
    width, so a correct key always leaves the top byte zero. Taking 256 bytes
    per block instead shifts everything by one and yields a header that parses
    into nonsense rather than failing.
    """
    try:
        modulus, exponent = RSA_KEYS[archive]
    except KeyError:
        raise Bhd5Error(
            f"no RSA key for archive {archive!r} — a game update that adds an "
            f"archive needs its key added to RSA_KEYS") from None
    if not blob or len(blob) % _RSA_BLOCK:
        raise Bhd5Error(
            f"{archive}.bhd is {len(blob)} bytes, not a whole number of "
            f"{_RSA_BLOCK}-byte RSA blocks — the file is truncated")
    out = bytearray()
    for off in range(0, len(blob), _RSA_BLOCK):
        plain = pow(int.from_bytes(blob[off:off + _RSA_BLOCK], "big"),
                    exponent, modulus).to_bytes(_RSA_BLOCK, "big")
        if plain[0]:
            # The only integrity signal RSA gives us for free. Without it a
            # wrong key produces plausible-looking garbage and every lookup
            # then reports "file not in this archive".
            raise Bhd5Error(
                f"the RSA key for {archive} did not decrypt it — a game patch "
                f"may have rotated the key")
        out += plain[1:]
    return bytes(out)


def _aes_key(plain, offset):
    """The per-file AES key and the byte ranges it covers, or (None, ()).

    Layout at `offset`: a 16-byte key, an int32 range count, then that many
    (int64 start, int64 end) pairs. A range of (-1, -1) is the unused sentinel.
    """
    if offset <= 0 or offset + 20 > len(plain):
        return None, ()
    key = plain[offset:offset + BLOCK]
    (count,) = struct.unpack_from("<i", plain, offset + BLOCK)
    ranges, pos = [], offset + BLOCK + 4
    for _ in range(max(count, 0)):
        start, end = struct.unpack_from("<qq", plain, pos)
        pos += 16
        if start >= 0 and end > start:
            ranges.append((start, end))
    return (key, tuple(ranges)) if ranges else (None, ())


def parse(plain):
    """Read a decrypted BHD5 index into `Index`."""
    if plain[:4] != b"BHD5":
        raise Bhd5Error(
            f"not a BHD5 index (magic {plain[:4]!r}) — the RSA step produced "
            f"the wrong bytes")
    bucket_count, bucket_offset = struct.unpack_from("<ii", plain, 16)
    (salt_len,) = struct.unpack_from("<i", plain, 24)
    salt = plain[28:28 + salt_len].decode("ascii", "replace")

    entries = {}
    pos = bucket_offset
    for _ in range(bucket_count):
        count, offset = struct.unpack_from("<ii", plain, pos)
        pos += 8
        for i in range(count):
            name_hash, padded, unpadded, data_off, _sha, aes_off = \
                _ENTRY.unpack_from(plain, offset + i * _ENTRY.size)
            key, ranges = _aes_key(plain, aes_off)
            entries[name_hash] = Entry(name_hash, padded, unpadded, data_off,
                                       key, ranges)
    return Index(salt, bucket_count, entries)


_INDEX_CACHE = {}


def load_index(game_dir, archive):
    """Parse one archive's index, remembering the result.

    Decrypting an index is thousands of modular exponentiations -- around a
    second for Data0 and several for the larger ones -- and a merge asks for
    more than one file. The cache is keyed on the file's identity so a game
    update invalidates it rather than serving a stale index.
    """
    from pathlib import Path
    path = Path(game_dir) / f"{archive}.bhd"
    try:
        stat = path.stat()
    except OSError as exc:
        raise Bhd5Error(f"cannot read {path}: {exc}") from exc
    token = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _INDEX_CACHE.get(token)
    if cached is None:
        cached = parse(decrypt_header(path.read_bytes(), archive))
        _INDEX_CACHE[token] = cached
    return cached


def _decrypt_payload(blob, entry):
    """Undo the per-file AES over the ranges the index names."""
    if entry.key is None:
        return blob
    out = bytearray(blob)
    for start, end in entry.ranges:
        lo = start - (start % BLOCK)
        hi = min(len(out), end + (-end % BLOCK))
        if hi > lo:
            out[lo:hi] = aes.decrypt_ecb(entry.key, bytes(out[lo:hi]))
    return bytes(out)


def find(game_dir, rel_path):
    """Locate `rel_path`, returning (archive, Entry), or None if nowhere."""
    wanted = path_hash(normalise(rel_path))
    for archive in ARCHIVES:
        try:
            index = load_index(game_dir, archive)
        except Bhd5Error:
            continue        # an archive this install doesn't ship
        entry = index.entries.get(wanted)
        if entry is not None:
            return archive, entry
    return None


def read_file(game_dir, rel_path):
    """The bytes of one game-relative path, straight out of the archives.

    Raises rather than returning empty on a miss: a caller using this as a
    merge base must never silently proceed from a file it failed to find.
    """
    from pathlib import Path
    hit = find(game_dir, rel_path)
    if hit is None:
        raise Bhd5Error(
            f"{normalise(rel_path)} is not in any of "
            f"{', '.join(ARCHIVES)} — check the spelling against the game's "
            f"own layout, which is lowercase and slash-separated")
    archive, entry = hit
    bdt = Path(game_dir) / f"{archive}.bdt"
    # Bounded: seek to the entry and take only its padded size. These files run
    # to 20 GB and must never be read whole.
    with open(bdt, "rb") as fh:
        fh.seek(entry.offset)
        blob = fh.read(entry.padded_size)
    if len(blob) != entry.padded_size:
        raise Bhd5Error(
            f"{archive}.bdt ended early reading {normalise(rel_path)}: wanted "
            f"{entry.padded_size} bytes at {entry.offset}, got {len(blob)}")
    blob = _decrypt_payload(blob, entry)
    # unpadded_size is 0 for entries the index doesn't track that closely; the
    # padded buffer is then the best answer available and DCX carries its own
    # length anyway.
    return blob[:entry.unpadded_size] if entry.unpadded_size else blob


def find_game_archives_dir():
    """The installed game's directory, or None when it isn't on this machine.

    Lets the real-file tests skip cleanly on a box with no Elden Ring, the same
    bargain the vendor-archive tests make.
    """
    try:
        from .. import paths
        game = paths.find_game_dir(paths.find_steam_root())
    except Exception:
        return None
    return game if game and (game / "Data0.bhd").exists() else None
