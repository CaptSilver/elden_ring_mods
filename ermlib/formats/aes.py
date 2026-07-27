"""AES-256-CBC, the outermost layer of Elden Ring's regulation.bin.

Pure stdlib on purpose. The alternatives were a third-party dependency (`ermlib`
has none) or shelling out to `openssl`, and neither is worth it here: AES wraps
the *compressed* DCX, roughly 2 MB, not the 54 MB payload inside it, so a Python
implementation costs a couple of seconds once per merge.

Every table below is generated from its algebraic definition rather than typed
out as a literal. A mistyped byte in a 256-entry S-box yields a cipher that
still round-trips against itself and disagrees with the rest of the world only
on data you didn't write — the kind of bug a naive test suite never sees.
"""
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


def encrypt_cbc(key, iv, plaintext):
    """CBC-encrypt `plaintext`. No padding: the input must already be blocks."""
    _check(key, iv, plaintext)
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


def decrypt_cbc(key, iv, ciphertext):
    """CBC-decrypt `ciphertext`. No padding is stripped — see `encrypt_cbc`."""
    _check(key, iv, ciphertext)
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
