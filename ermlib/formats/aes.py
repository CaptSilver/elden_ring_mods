"""AES-256-CBC, the outermost layer of Elden Ring's regulation.bin.

Two backends, tried in order: the system libcrypto through ctypes, then a pure
Python implementation. The native one matters more than it looks — AES wraps the
*compressed* DCX, about 2 MB, and a merge of six regulation-carrying mods runs
that through decrypt dozens of times. In Python each pass is ~5.5 s, so an
`erm apply` spends minutes here; libcrypto does the same 2 MB in under a
millisecond, byte for byte the same output.

The Python implementation stays as the fallback rather than becoming dead code:
`ermlib` has no third-party dependencies and must keep working on a box where
`crypto` doesn't resolve. `dcx.py` runs the same backend chain for zstd.

Every table below is generated from its algebraic definition rather than typed
out as a literal. A mistyped byte in a 256-entry S-box yields a cipher that
still round-trips against itself and disagrees with the rest of the world only
on data you didn't write — the kind of bug a naive test suite never sees.
"""
import ctypes
import ctypes.util

from ..errors import ErmError

BLOCK = 16
_KEY_BYTES = 32     # AES-256
_ROUNDS = 14        # Nr for a 256-bit key
_NK = 8             # key length in 32-bit words


class AesError(ErmError):
    """A key, IV, or buffer was the wrong shape for AES-CBC."""


def _xtime(a):
    """Multiply by x in GF(2^8) modulo the AES polynomial 0x11B."""
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a, b):
    """Multiply two GF(2^8) elements (Russian-peasant, constant shape)."""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _build_sbox():
    """The AES S-box: multiplicative inverse in GF(2^8), then an affine map.

    Deriving it means the only way to get it wrong is to get the *definition*
    wrong, which the NIST vectors catch immediately.
    """
    # inverse[x] = x^254, since the multiplicative group has order 255.
    inverse = [0] * 256
    for x in range(1, 256):
        y = x
        for _ in range(253):        # x^254 == x^(255-1)
            y = _mul(y, x)
        inverse[x] = y
    sbox = []
    for x in range(256):
        b = inverse[x]
        s = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            s ^= b
        sbox.append(s ^ 0x63)
    return sbox


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

# Column-mix coefficients, precomputed so the round functions stay table lookups.
_MUL = {c: [_mul(x, c) for x in range(256)] for c in (2, 3, 9, 11, 13, 14)}

# Round constants: 1, 2, 4, 8, ... in GF(2^8). Only 7 are reachable at Nk=8.
_RCON = [1]
for _ in range(9):
    _RCON.append(_xtime(_RCON[-1]))


def _expand_key(key):
    """Return `_ROUNDS + 1` round keys of 16 bytes each."""
    words = [list(key[i:i + 4]) for i in range(0, _KEY_BYTES, 4)]
    total = 4 * (_ROUNDS + 1)
    for i in range(_NK, total):
        t = list(words[i - 1])
        if i % _NK == 0:
            t = t[1:] + t[:1]                       # RotWord
            t = [SBOX[b] for b in t]                # SubWord
            t[0] ^= _RCON[i // _NK - 1]
        elif i % _NK == 4:
            # AES-256 only: an extra SubWord halfway through each expansion step.
            t = [SBOX[b] for b in t]
        words.append([a ^ b for a, b in zip(words[i - _NK], t)])
    return [bytes(b for w in words[r * 4:r * 4 + 4] for b in w)
            for r in range(_ROUNDS + 1)]


def _add_round_key(state, rk):
    for i in range(BLOCK):
        state[i] ^= rk[i]


def _encrypt_block(state, round_keys):
    """In-place forward cipher on a 16-byte `state` laid out column-major."""
    _add_round_key(state, round_keys[0])
    for rnd in range(1, _ROUNDS + 1):
        for i in range(BLOCK):
            state[i] = SBOX[state[i]]
        # ShiftRows: row r rotates left by r. Byte i holds row i%4, column i//4.
        state[:] = bytearray(state[(i % 4) + 4 * ((i // 4 + i % 4) % 4)]
                             for i in range(BLOCK))
        if rnd != _ROUNDS:              # the last round omits MixColumns
            m2, m3 = _MUL[2], _MUL[3]
            for c in range(0, BLOCK, 4):
                a0, a1, a2, a3 = state[c], state[c + 1], state[c + 2], state[c + 3]
                state[c] = m2[a0] ^ m3[a1] ^ a2 ^ a3
                state[c + 1] = a0 ^ m2[a1] ^ m3[a2] ^ a3
                state[c + 2] = a0 ^ a1 ^ m2[a2] ^ m3[a3]
                state[c + 3] = m3[a0] ^ a1 ^ a2 ^ m2[a3]
        _add_round_key(state, round_keys[rnd])


def _decrypt_block(state, round_keys):
    """In-place inverse cipher, mirroring `_encrypt_block` step for step."""
    _add_round_key(state, round_keys[_ROUNDS])
    for rnd in range(_ROUNDS - 1, -1, -1):
        # InvShiftRows: row r rotates right by r.
        state[:] = bytearray(state[(i % 4) + 4 * ((i // 4 - i % 4) % 4)]
                             for i in range(BLOCK))
        for i in range(BLOCK):
            state[i] = INV_SBOX[state[i]]
        _add_round_key(state, round_keys[rnd])
        if rnd != 0:                    # mirrors the omitted MixColumns above
            m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
            for c in range(0, BLOCK, 4):
                a0, a1, a2, a3 = state[c], state[c + 1], state[c + 2], state[c + 3]
                state[c] = m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3]
                state[c + 1] = m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3]
                state[c + 2] = m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3]
                state[c + 3] = m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]


def _check(key, iv, data):
    if len(key) != _KEY_BYTES:
        raise AesError(f"AES-256 needs a key of 32 bytes, got {len(key)}")
    if len(iv) != BLOCK:
        raise AesError(f"CBC needs an IV of 16 bytes, got {len(iv)}")
    if len(data) % BLOCK:
        raise AesError(
            f"CBC works in whole 16-byte blocks; got {len(data)} bytes, which is "
            f"not a multiple of 16 — the caller sliced the buffer wrong")


def _encrypt_cbc_py(key, iv, plaintext):
    """CBC-encrypt block by block, chaining each into the next."""
    round_keys = _expand_key(key)
    prev = bytearray(iv)
    out = bytearray()
    for off in range(0, len(plaintext), BLOCK):
        state = bytearray(plaintext[off:off + BLOCK])
        for i in range(BLOCK):
            state[i] ^= prev[i]
        _encrypt_block(state, round_keys)
        out += state
        prev = state
    return bytes(out)


def _decrypt_cbc_py(key, iv, ciphertext):
    """The mirror of `_encrypt_cbc_py`: decipher, then XOR the ciphertext
    block that came before."""
    round_keys = _expand_key(key)
    prev = bytes(iv)
    out = bytearray()
    for off in range(0, len(ciphertext), BLOCK):
        block = ciphertext[off:off + BLOCK]
        state = bytearray(block)
        _decrypt_block(state, round_keys)
        for i in range(BLOCK):
            state[i] ^= prev[i]
        out += state
        prev = block
    return bytes(out)


def _pure_python():
    """The implementation above. Needs nothing installed, so it never fails."""
    return _encrypt_cbc_py, _decrypt_cbc_py


def _ctypes_libcrypto():
    """OpenSSL's EVP interface through ctypes — no Python package required.

    Every box that can talk TLS has libcrypto, and erm already drives the
    vendored Kraken decoder and libzstd this way. `restype` on the two functions
    returning pointers is not optional: ctypes defaults to c_int, which lops the
    top half off a 64-bit pointer and segfaults on first use.
    """
    name = ctypes.util.find_library("crypto") or "libcrypto.so.3"
    try:
        lib = ctypes.CDLL(name)
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_aes_256_cbc.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
        directions = {}
        for way in ("Encrypt", "Decrypt"):
            init = getattr(lib, f"EVP_{way}Init_ex")
            init.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_char_p, ctypes.c_char_p]
            init.restype = ctypes.c_int
            update = getattr(lib, f"EVP_{way}Update")
            update.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_int), ctypes.c_char_p,
                               ctypes.c_int]
            update.restype = ctypes.c_int
            final = getattr(lib, f"EVP_{way}Final_ex")
            final.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_int)]
            final.restype = ctypes.c_int
            directions[way] = (init, update, final)
    except (OSError, AttributeError) as exc:
        raise ImportError(f"libcrypto not usable via ctypes ({exc})") from exc

    def check(ok, what):
        if ok != 1:
            raise AesError(f"libcrypto refused the AES-256-CBC {what}")

    def run(way, key, iv, data):
        init, update, final = directions[way]
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise AesError("libcrypto could not allocate an AES-256-CBC context")
        try:
            check(init(ctx, lib.EVP_aes_256_cbc(), None, bytes(key), bytes(iv)),
                  "key and IV")
            # The module's contract is that no padding is added or stripped —
            # regulation.pack appends its own block and sizes the DCX frame
            # around it. Left on, EVP would silently add a 17th block.
            check(lib.EVP_CIPHER_CTX_set_padding(ctx, 0), "padding mode")
            data = bytes(data)
            out = ctypes.create_string_buffer(len(data) + BLOCK)
            written = ctypes.c_int(0)
            check(update(ctx, out, ctypes.byref(written), data, len(data)), "body")
            total = written.value
            check(final(ctx,
                        ctypes.cast(ctypes.addressof(out) + total, ctypes.c_void_p),
                        ctypes.byref(written)), "final block")
            return out.raw[:total + written.value]
        finally:
            lib.EVP_CIPHER_CTX_free(ctypes.c_void_p(ctx))

    return (lambda key, iv, plaintext: run("Encrypt", key, iv, plaintext),
            lambda key, iv, ciphertext: run("Decrypt", key, iv, ciphertext))


# Native first: the Python fallback always loads, so putting it anywhere but
# last would mean nobody ever reaches libcrypto.
_BACKENDS = (_ctypes_libcrypto, _pure_python)


def _cipher():
    """(encrypt, decrypt) from the first backend that loads.

    Resolved on use, never at import, so a box with no libcrypto doesn't pay for
    the lookup on every erm command that has nothing to do with regulation.bin.
    """
    for backend in _BACKENDS:
        try:
            return backend()
        except ImportError:
            continue
    raise AesError(
        "no AES backend loaded — the pure-Python one needs nothing installed, "
        "so this means _BACKENDS was replaced with something that can fail")


def encrypt_cbc(key, iv, plaintext):
    """CBC-encrypt `plaintext`. No padding: the input must already be blocks."""
    _check(key, iv, plaintext)
    return _cipher()[0](key, iv, plaintext)


def decrypt_cbc(key, iv, ciphertext):
    """CBC-decrypt `ciphertext`. No padding is stripped — see `encrypt_cbc`.

    The lengths are checked here rather than in each backend: libcrypto takes
    the key as a bare pointer and reads 32 bytes from it whatever the caller
    passed, so a short key would be an out-of-bounds read, not an error.
    """
    _check(key, iv, ciphertext)
    return _cipher()[1](key, iv, ciphertext)
