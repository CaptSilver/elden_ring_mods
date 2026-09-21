"""BHD5/BDT: reading a file out of the game's own packed archives.

The unit tests build a synthetic archive so they run anywhere. The real-file
tests skip when the game isn't installed, the same bargain the vendor-archive
tests make -- see tests that read vendor/.
"""
import struct

import pytest

from ermlib.formats import bhd5
from ermlib.formats.bhd5 import Bhd5Error

GAME = bhd5.find_game_archives_dir()

needs_game = pytest.mark.skipif(
    GAME is None, reason="Elden Ring is not installed on this machine")

# Measured from the real Data0.bhd index, not computed by the code under test:
# this exact hash is the entry that yields the game's menu text.
MENU_PATH = "msg/engus/menu_dlc02.msgbnd.dcx"
MENU_HASH = 0xF6CA09AEB7E65CC9


def test_path_hash_matches_the_value_measured_from_the_real_index():
    assert bhd5.path_hash(bhd5.normalise(MENU_PATH)) == MENU_HASH


def test_normalise_forces_a_leading_slash():
    """The single most load-bearing rule. Without the slash the hash is a
    different number that is simply absent from the table, so a lookup fails
    with 'not found' rather than anything that points at normalisation."""
    assert bhd5.normalise("msg/engus/x.dcx") == "/msg/engus/x.dcx"
    assert bhd5.normalise("/msg/engus/x.dcx") == "/msg/engus/x.dcx"


def test_normalise_lowercases_and_squares_the_separators():
    assert bhd5.normalise("MSG\\ENGUS\\X.DCX") == "/msg/engus/x.dcx"


def test_a_path_without_the_leading_slash_hashes_differently():
    """Guards the rule above: if these ever collide the test proves nothing."""
    assert bhd5.path_hash("msg/engus/menu_dlc02.msgbnd.dcx") != MENU_HASH


def test_path_hash_is_the_documented_recurrence():
    """h = char + 133*h, accumulated 64-bit. Spot-checked on a short string so
    a rewrite of the loop can't quietly change the constant."""
    expect = 0
    for ch in "/abc":
        expect = (ord(ch) + 133 * expect) & 0xFFFFFFFFFFFFFFFF
    assert bhd5.path_hash("/abc") == expect


def test_path_hash_wraps_at_64_bits():
    """A long path overflows; Python ints don't, so the mask is load-bearing."""
    assert bhd5.path_hash("/" + "a" * 200) < 1 << 64


def _synthetic_bhd5(entries, bucket_count=4, salt=b"GR_test"):
    """Build a BHD5 index the parser should accept. Mirrors the real layout:
    fixed header, bucket directory, then the 40-byte entry records."""
    head_len = 28 + len(salt)
    bucket_dir = head_len
    entry_area = bucket_dir + bucket_count * 8
    buckets = [[] for _ in range(bucket_count)]
    for e in entries:
        buckets[e[0] % bucket_count].append(e)

    blob = bytearray()
    off = entry_area
    dir_bytes = bytearray()
    entry_bytes = bytearray()
    for b in buckets:
        dir_bytes += struct.pack("<ii", len(b), off)
        for name_hash, padded, unpadded, data_off in b:
            entry_bytes += struct.pack("<QiiqQQ", name_hash, padded, unpadded,
                                       data_off, 0, 0)
            off += 40
    blob += b"BHD5"
    blob += struct.pack("<bbbb", -1, 1, 0, 0)
    blob += struct.pack("<ii", 1, head_len + len(dir_bytes) + len(entry_bytes))
    blob += struct.pack("<ii", bucket_count, bucket_dir)
    blob += struct.pack("<i", len(salt))
    blob += salt
    blob += dir_bytes + entry_bytes
    return bytes(blob)


def test_parse_reads_every_entry_out_of_its_bucket():
    ents = [(0x1111111111111111, 32, 30, 0),
            (0x2222222222222222, 64, 64, 32),
            (0x3333333333333333, 16, 5, 96)]
    idx = bhd5.parse(_synthetic_bhd5(ents))
    assert set(idx.entries) == {e[0] for e in ents}
    assert idx.entries[0x2222222222222222].padded_size == 64
    assert idx.entries[0x3333333333333333].offset == 96


def test_parse_keeps_padded_and_unpadded_sizes_apart():
    """The .bdt slice must be cut to the padded size or AES gets a partial
    block; the payload is then trimmed to the unpadded one."""
    idx = bhd5.parse(_synthetic_bhd5([(0x55, 48, 33, 0)]))
    e = idx.entries[0x55]
    assert (e.padded_size, e.unpadded_size) == (48, 33)


def test_parse_refuses_something_that_is_not_a_bhd5():
    with pytest.raises(Bhd5Error):
        bhd5.parse(b"NOPE" + bytes(64))


def test_parse_reports_the_salt():
    assert bhd5.parse(_synthetic_bhd5([], salt=b"GR_other")).salt == "GR_other"


def test_every_shipped_archive_has_a_key():
    """A new archive in a future patch must fail loudly here, not silently be
    skipped during a lookup."""
    assert set(bhd5.ARCHIVES) == set(bhd5.RSA_KEYS)


def test_decrypt_header_rejects_a_wrong_key():
    """A wrong key leaves a non-zero leading byte on the modexp result. That is
    the only cheap integrity signal RSA gives us here, so it must be checked --
    otherwise a future key change yields garbage that parses as 'no such file'.
    """
    with pytest.raises(Bhd5Error):
        bhd5.decrypt_header(b"\xff" * 256, "Data0")


def test_decrypt_header_rejects_a_truncated_archive():
    with pytest.raises(Bhd5Error):
        bhd5.decrypt_header(b"\x00" * 100, "Data0")


# --- against the installed game -------------------------------------------

@needs_game
@pytest.mark.parametrize("archive", bhd5.ARCHIVES)
def test_every_archive_header_decrypts_and_parses(archive):
    idx = bhd5.load_index(GAME, archive)
    assert idx.entries, f"{archive} produced an empty index"


@needs_game
def test_reads_the_games_own_menu_text():
    """The end-to-end path this module exists for: RSA -> BHD5 -> hash lookup
    -> bounded .bdt read -> AES -> a DCX the repo's own readers accept."""
    from ermlib.formats import bnd4, dcx, fmg
    raw = bhd5.read_file(GAME, MENU_PATH)
    assert raw[:4] == b"DCX\x00"
    tables = {}
    for e in bnd4.read(dcx.read(raw)):
        tables[(e.name or "").split("\\")[-1]] = fmg.read(e.data)
    talk = tables["EventTextForTalk.fmg"]
    assert talk, "EventTextForTalk.fmg came back empty"
    # 1.17 added the spectral-steed appearance menu. Its presence is what makes
    # the game's copy worth reading instead of a mod's older snapshot.
    assert talk.get(20010070) == "Change spectral steed appearance"


@needs_game
def test_a_missing_path_is_an_error_not_empty_bytes():
    """Silently returning nothing would let a merge mount a file built from a
    base that was never found."""
    with pytest.raises(Bhd5Error):
        bhd5.read_file(GAME, "msg/engus/there-is-no-such-file.dcx")


@needs_game
def test_lookup_is_case_insensitive_like_the_game():
    assert bhd5.read_file(GAME, "MSG/ENGUS/MENU_DLC02.MSGBND.DCX")[:4] == b"DCX\x00"
