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


def test_encrypt_matches_the_nist_vector():
    assert aes.encrypt_cbc(NIST_KEY, NIST_IV, NIST_PLAIN) == NIST_CIPHER


def test_decrypt_matches_the_nist_vector():
    assert aes.decrypt_cbc(NIST_KEY, NIST_IV, NIST_CIPHER) == NIST_PLAIN


def test_round_trip_over_many_blocks():
    # Chaining bugs hide in single-block tests: every block after the first
    # depends on the previous ciphertext block, so a mistake there only shows
    # up once there is a previous block to get wrong.
    data = bytes(range(256)) * 8
    out = aes.decrypt_cbc(NIST_KEY, NIST_IV, aes.encrypt_cbc(NIST_KEY, NIST_IV, data))
    assert out == data


def test_iv_changes_the_ciphertext():
    other_iv = bytes(16)
    assert aes.encrypt_cbc(NIST_KEY, other_iv, NIST_PLAIN) != NIST_CIPHER


def test_empty_input_is_empty_output():
    assert aes.encrypt_cbc(NIST_KEY, NIST_IV, b"") == b""
    assert aes.decrypt_cbc(NIST_KEY, NIST_IV, b"") == b""


def test_rejects_a_key_that_is_not_256_bit():
    # Silently accepting a short key would encrypt with a key nobody chose.
    with pytest.raises(AesError, match="32 bytes"):
        aes.encrypt_cbc(NIST_KEY[:16], NIST_IV, NIST_PLAIN)


def test_rejects_a_bad_iv_length():
    with pytest.raises(AesError, match="16 bytes"):
        aes.decrypt_cbc(NIST_KEY, NIST_IV[:8], NIST_CIPHER)


def test_rejects_input_that_is_not_a_whole_number_of_blocks():
    # CBC has no padding here by design — the caller owns that. A partial block
    # means the caller sliced wrong, and truncating it silently would corrupt
    # the tail of a regulation.bin.
    with pytest.raises(AesError, match="multiple of 16"):
        aes.decrypt_cbc(NIST_KEY, NIST_IV, NIST_CIPHER[:-1])
