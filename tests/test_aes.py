import random

import pytest

from ermlib.formats import aes
from ermlib.formats.aes import AesError

# NIST SP 800-38A, F.2.5/F.2.6 (CBC-AES256). Verified against an independent
# implementation before being written down here, so a failure means our code is
# wrong, not that the vector was mistyped from memory.
NIST_KEY = bytes.fromhex(
    "603deb1015ca71be2b73aef0857d7781"
    "1f352c073b6108d72d9810a30914dff4")
NIST_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
NIST_PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710")
NIST_CIPHER = bytes.fromhex(
    "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
    "9cfc4e967edb808d679f777bc6702c7d"
    "39f23369a9d9bacfa530e26304231461"
    "b2eb05e2c39be9fcda6c19078c6a9d1b")


def _load(backend):
    """The (encrypt, decrypt) pair for one backend, or None if it isn't here."""
    try:
        return backend()
    except ImportError:
        return None


@pytest.fixture(params=[b.__name__ for b in aes._BACKENDS])
def backend(request, monkeypatch):
    """Run the test once per AES backend this machine can actually load.

    Pinning `_BACKENDS` to one entry means the assertions go through the public
    functions callers use, not through a hand-picked pair — a backend that is
    correct in isolation but never selected would still be a bug.
    """
    chosen = next(b for b in aes._BACKENDS if b.__name__ == request.param)
    if _load(chosen) is None:
        pytest.skip(f"{request.param} is not available on this machine")
    monkeypatch.setattr(aes, "_BACKENDS", (chosen,))
    return chosen


def test_encrypt_matches_the_nist_vector(backend):
    assert aes.encrypt_cbc(NIST_KEY, NIST_IV, NIST_PLAIN) == NIST_CIPHER


def test_decrypt_matches_the_nist_vector(backend):
    assert aes.decrypt_cbc(NIST_KEY, NIST_IV, NIST_CIPHER) == NIST_PLAIN


def test_round_trip_over_many_blocks(backend):
    # Chaining bugs hide in single-block tests: every block after the first
    # depends on the previous ciphertext block, so a mistake there only shows
    # up once there is a previous block to get wrong.
    data = bytes(range(256)) * 8
    out = aes.decrypt_cbc(NIST_KEY, NIST_IV, aes.encrypt_cbc(NIST_KEY, NIST_IV, data))
    assert out == data


def test_iv_changes_the_ciphertext(backend):
    other_iv = bytes(16)
    assert aes.encrypt_cbc(NIST_KEY, other_iv, NIST_PLAIN) != NIST_CIPHER


def test_empty_input_is_empty_output(backend):
    assert aes.encrypt_cbc(NIST_KEY, NIST_IV, b"") == b""
    assert aes.decrypt_cbc(NIST_KEY, NIST_IV, b"") == b""


def test_rejects_a_key_that_is_not_256_bit(backend):
    # Silently accepting a short key would encrypt with a key nobody chose.
    with pytest.raises(AesError, match="32 bytes"):
        aes.encrypt_cbc(NIST_KEY[:16], NIST_IV, NIST_PLAIN)


def test_rejects_a_bad_iv_length(backend):
    with pytest.raises(AesError, match="16 bytes"):
        aes.decrypt_cbc(NIST_KEY, NIST_IV[:8], NIST_CIPHER)


def test_rejects_input_that_is_not_a_whole_number_of_blocks(backend):
    # CBC has no padding here by design — the caller owns that. A partial block
    # means the caller sliced wrong, and truncating it silently would corrupt
    # the tail of a regulation.bin.
    with pytest.raises(AesError, match="multiple of 16"):
        aes.decrypt_cbc(NIST_KEY, NIST_IV, NIST_CIPHER[:-1])


def test_encrypting_adds_no_padding_block(backend):
    # The caller owns padding: regulation.pack appends its own PKCS#7 block and
    # the DCX frame that follows is sized from the plaintext. A backend quietly
    # adding a block would push a regulation.bin 16 bytes past what it declares.
    assert len(aes.encrypt_cbc(NIST_KEY, NIST_IV, NIST_PLAIN)) == len(NIST_PLAIN)


def test_the_native_backend_is_tried_before_pure_python():
    """Order is the whole point of the chain: pure Python always loads, so if it
    came first the native one would never run and the merge would stay minutes
    long on a machine that has libcrypto."""
    assert aes._BACKENDS[0] is aes._ctypes_libcrypto
    assert aes._BACKENDS[-1] is aes._pure_python


def test_falls_back_to_pure_python_when_the_native_library_is_missing(monkeypatch):
    """A box without libcrypto still merges, just slowly — nothing here is a
    hard dependency."""
    def unavailable():
        raise ImportError("no libcrypto here")

    monkeypatch.setattr(aes, "_BACKENDS", (unavailable, aes._pure_python))
    assert aes.encrypt_cbc(NIST_KEY, NIST_IV, NIST_PLAIN) == NIST_CIPHER


def test_no_usable_backend_is_reported_rather_than_returning_junk(monkeypatch):
    monkeypatch.setattr(aes, "_BACKENDS", ())
    with pytest.raises(AesError, match="no AES backend"):
        aes.encrypt_cbc(NIST_KEY, NIST_IV, NIST_PLAIN)


def _installed_backends():
    return [(b.__name__, pair) for b in aes._BACKENDS
            if (pair := _load(b)) is not None]


def test_every_installed_backend_produces_the_same_bytes():
    """The native and pure paths have to be interchangeable down to the byte: a
    regulation.bin is rebuilt on each machine and co-op compares the file, so a
    box that took the other backend must not produce a different merge."""
    backends = _installed_backends()
    if len(backends) < 2:
        pytest.skip(f"only one AES backend here: {[n for n, _ in backends]}")
    rng = random.Random(0xE1DE7)
    for size in (0, 16, 32, 4096, 65536):
        data = bytes(rng.getrandbits(8) for _ in range(size))
        iv = bytes(rng.getrandbits(8) for _ in range(aes.BLOCK))
        ciphers = {name: encrypt(NIST_KEY, iv, data)
                   for name, (encrypt, _decrypt) in backends}
        assert len(set(ciphers.values())) == 1, {
            n: len(c) for n, c in ciphers.items()}
        for name, (_encrypt, decrypt) in backends:
            assert decrypt(NIST_KEY, iv, next(iter(ciphers.values()))) == data, name
