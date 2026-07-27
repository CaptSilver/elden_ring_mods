import struct
import zlib

import pytest

from ermlib.formats import dcx


def test_dflt_round_trip():
    payload = b"BND4" + bytes(range(256)) * 40
    blob = dcx.write_dflt(payload)
    assert blob[:4] == b"DCX\x00"
    assert blob[0x28:0x2c] == b"DFLT"
    assert dcx.read(blob) == payload


def test_write_rejects_level_zero():
    """The game's DCX reader validates the level byte as 1-9 (dec al; cmp al,8;
    ja fail). zlib level 0 writes a 0 there and the file is rejected at load."""
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=0)


def test_write_rejects_level_above_nine():
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=10)


def test_header_fields_match_a_known_good_layout():
    """Byte-for-byte agreement with the DFLT headers Clever's Moveset ships,
    which the game loads today. Sizes are the only fields allowed to differ."""
    payload = b"x" * 1000
    blob = dcx.write_dflt(payload)
    assert blob[0x00:0x04] == b"DCX\x00"
    assert blob[0x18:0x1c] == b"DCS\x00"
    assert blob[0x24:0x28] == b"DCP\x00"
    assert blob[0x2c:0x30] == (0x20).to_bytes(4, "big")
    assert blob[0x30] == 9
    assert int.from_bytes(blob[0x04:0x08], "big") <= 0x11000
    assert int.from_bytes(blob[0x1c:0x20], "big") == len(payload)


def test_read_rejects_a_non_dcx():
    with pytest.raises(dcx.DcxError):
        dcx.read(b"NOTDCX" + b"\x00" * 200)


def test_read_rejects_a_truncated_header():
    """A file cut short before the fixed-size header even ends can't be
    unpacked at all -- struct.unpack_from on the compression fields would
    raise a bare struct.error instead of DcxError. Magic intact, everything
    after it gone, so only the header-length check can catch this."""
    with pytest.raises(dcx.DcxError, match="header is truncated"):
        dcx.read(b"DCX\x00" + b"\x00" * 10)


def test_read_rejects_an_unknown_compression():
    # Deliberately a method that is not a real DCX codec at all. This used to
    # say ZSTD, which was accurate until ZSTD was implemented and then quietly
    # started exercising the decompressor instead of the rejection path.
    blob = bytearray(dcx.write_dflt(b"payload"))
    blob[0x28:0x2c] = b"ZZZZ"
    with pytest.raises(dcx.DcxError, match="unsupported"):
        dcx.read(bytes(blob))


def test_read_rejects_a_truncated_body():
    """A file cut short mid-download or mid-extraction still has a valid
    header claiming the original compressed size. Without this check, zlib
    would be handed a partial stream and either raise its own opaque error
    deep in the decompressor or, worse, silently decompress a truncated
    prefix and hand back incomplete game data as if it were valid."""
    blob = bytearray(dcx.write_dflt(b"payload" * 50))
    truncated = bytes(blob[:-10])  # header still claims the full compressed size
    with pytest.raises(dcx.DcxError):
        dcx.read(truncated)


def test_read_rejects_a_dflt_size_mismatch():
    """If the decompressed size didn't have to match the header's claim, a
    corrupted or hand-edited size field would go unnoticed — anyone sizing
    a buffer off this field, or relying on it to sanity-check the payload,
    would get silently wrong data instead of a loud failure."""
    payload = b"payload" * 30
    blob = bytearray(dcx.write_dflt(payload))
    struct.pack_into(">I", blob, 0x1C, len(payload) + 1)
    with pytest.raises(dcx.DcxError):
        dcx.read(bytes(blob))


def test_read_rejects_a_compressed_size_that_overruns_the_buffer():
    """A corrupted or hostile header can claim megabytes of compressed data
    against a file that holds almost none. Python slicing doesn't raise on
    an out-of-range stop index, so without the length check this would pass
    a near-empty body straight to the decompressor instead of failing here."""
    blob = bytearray(dcx.write_dflt(b"x"))
    struct.pack_into(">I", blob, 0x20, 10_000_000)
    truncated = bytes(blob[:dcx.HEADER_SIZE])  # header intact, body entirely gone
    with pytest.raises(dcx.DcxError):
        dcx.read(truncated)


def test_zstd_round_trip():
    payload = b"BND4" + bytes(range(256)) * 40
    blob = dcx.write_zstd(payload)
    assert blob[:4] == b"DCX\x00"
    assert blob[0x28:0x2c] == b"ZSTD"
    assert dcx.read(blob) == payload


def test_zstd_frame_omits_the_content_size_flag():
    """Real regulation.bin frames carry FHD 0x00. The stdlib's ONE-SHOT
    compressor sets bit 7 (frame content size present), which no shipped file
    does; only the streaming compressor matches. Guarding it here because the
    one-shot call is the obvious way to write this and produces a frame that
    differs structurally from every file the game has ever loaded."""
    blob = dcx.write_zstd(b"x" * 4096)
    frame = blob[dcx.HEADER_SIZE:]
    assert frame[:4] == bytes.fromhex("28b52ffd")
    assert frame[4] & 0x80 == 0, f"frame header descriptor {frame[4]:#04x} sets the content-size flag"


def test_zstd_accepts_levels_the_dflt_path_rejects():
    """The 1-9 clamp is a property of the game's *zlib* branch. Shipped ZSTD
    regulations carry 15 (Clever's) and 21 (vanilla) in the level byte, so
    applying the zlib guard here would refuse to write a normal file."""
    blob = dcx.write_zstd(b"payload" * 100, level=15)
    assert blob[0x30] == 15
    assert dcx.read(blob) == b"payload" * 100


def test_zstd_header_records_both_sizes():
    payload = bytes(range(256)) * 100
    blob = dcx.write_zstd(payload)
    uncompressed, compressed = struct.unpack_from(">II", blob, 0x1c)
    assert uncompressed == len(payload)
    assert compressed == len(blob) - dcx.HEADER_SIZE


def test_read_rejects_a_truncated_zstd_body():
    blob = dcx.write_zstd(bytes(range(256)) * 100)
    with pytest.raises(dcx.DcxError, match="truncated"):
        dcx.read(blob[:-32])


def test_read_rejects_a_zstd_size_mismatch():
    """A frame that decompresses to a different length than the header claims
    means the container and its body disagree — refuse rather than hand back a
    payload that BND4 will then misparse."""
    payload = bytes(range(256)) * 100
    blob = bytearray(dcx.write_zstd(payload))
    struct.pack_into(">I", blob, 0x1c, len(payload) + 1)
    with pytest.raises(dcx.DcxError, match="ZSTD payload"):
        dcx.read(bytes(blob))


ZSTD_MOD_WINDOW_LOG = 16


def _frame_window_log(blob):
    frame = blob[dcx.HEADER_SIZE:]
    assert frame[:4] == bytes.fromhex("28b52ffd")
    assert frame[4] & 0x20 == 0, "single-segment frames encode the window differently"
    return 10 + (frame[5] >> 3)


def test_zstd_frame_window_stays_within_what_the_game_loads():
    """A regulation.bin whose frame demands a large window crashes the game on
    load, even with byte-identical content to one that works. Every regulation
    shipped by a mod tool declares windowLog 16; only vanilla's own file goes
    higher, and that one is never served through the loader's file override.

    The payload has to exceed the window for the encoder to record a real one,
    so a small test string would pass this vacuously.
    """
    blob = dcx.write_zstd(b"".join(bytes([i % 251]) * 97 for i in range(4000)))
    assert _frame_window_log(blob) <= ZSTD_MOD_WINDOW_LOG


def test_zstd_window_matches_the_byte_shipped_regulations_carry():
    """Pinned to the exact descriptor byte rather than just the bound, because
    matching what shipped files do is the whole basis for believing this is
    safe -- nothing here can verify it against the game."""
    blob = dcx.write_zstd(b"".join(bytes([i % 251]) * 97 for i in range(4000)))
    assert blob[dcx.HEADER_SIZE + 5] == 0x30


def _without_zstd(monkeypatch):
    """Simulate a machine with no zstd at all: an interpreter older than 3.14
    and none of the pip backends installed."""
    import importlib as _il
    real = _il.import_module
    blocked = {name for name, _adapt in dcx._BACKENDS}

    def missing(name, *args, **kwargs):
        if name in blocked:
            raise ImportError(f"No module named {name!r}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(dcx.importlib, "import_module", missing)


def test_the_other_codecs_still_work_without_the_zstd_module(monkeypatch):
    """compression.zstd arrived in Python 3.14 and SteamOS ships older. Importing
    it at module scope took EVERY erm command down there — not just merging, but
    anything that touches ermlib.formats at all. Only regulation.bin is ZSTD."""
    payload = b"BND4" + bytes(range(256)) * 10
    dflt = dcx.write_dflt(payload)
    _without_zstd(monkeypatch)
    assert dcx.read(dflt) == payload
    assert dcx.write_dflt(payload) == dflt


def test_writing_zstd_with_no_backend_says_what_to_install(monkeypatch):
    _without_zstd(monkeypatch)
    with pytest.raises(dcx.DcxError, match="pyzstd"):
        dcx.write_zstd(b"payload" * 100)


def test_reading_zstd_without_the_module_fails_as_a_dcx_error(monkeypatch):
    """An ImportError escaping here would surface as a crash rather than the
    ordinary 'this archive needs something you don't have' path callers handle."""
    blob = dcx.write_zstd(b"payload" * 100)
    _without_zstd(monkeypatch)
    with pytest.raises(dcx.DcxError, match="no zstd available"):
        dcx.read(blob)


def _available_backends():
    out = []
    for name, adapt in dcx._BACKENDS:
        try:
            out.append((name, adapt(dcx.importlib.import_module(name))))
        except ImportError:
            pass
    return out


# Deterministic and bigger than the 64 KiB window, so window_log actually shows
# up in the frame rather than being elided.
_PINNED_PAYLOAD = bytes((i * 7919 + (i >> 3)) & 0xFF for i in range(300000))
_PINNED_FRAME_SHA256 = "2d587b5079b7baaf1353de00d78a38c0b86f4799a0104db1be1bce234b54f5c4"


def test_the_stdlib_backend_is_preferred_when_present(monkeypatch):
    """Order matters for reproducibility, not correctness: whichever backend is
    first has to be the same one on every machine that has a choice."""
    assert dcx._BACKENDS[0][0] == "compression.zstd"


def test_every_installed_backend_agrees_byte_for_byte():
    """Co-op partners must end up with the same regulation.bin, and it is built
    on each machine rather than distributed. Two backends disagreeing here would
    mean two players generating different files from identical inputs."""
    backends = _available_backends()
    if len(backends) < 2:
        pytest.skip(f"only one zstd backend installed: {[n for n, _ in backends]}")
    frames = {}
    for name, (_decompress, compress, _err) in backends:
        frames[name] = compress(_PINNED_PAYLOAD, dcx.ZSTD_LEVEL, dcx.ZSTD_WINDOW_LOG)
    assert len(set(frames.values())) == 1, {n: len(f) for n, f in frames.items()}


def test_every_installed_backend_reads_what_the_others_write():
    for name, (decompress, compress, _err) in _available_backends():
        frame = compress(_PINNED_PAYLOAD, dcx.ZSTD_LEVEL, dcx.ZSTD_WINDOW_LOG)
        for other, (other_decompress, _c, _e) in _available_backends():
            assert other_decompress(frame, len(_PINNED_PAYLOAD)) == _PINNED_PAYLOAD, (
                f"{other} could not read {name}'s frame")


def test_the_compressed_frame_matches_the_pinned_digest():
    """A failure here is not a bug in erm — it means this machine's libzstd emits
    different bytes than the one this digest was taken on. That matters because
    a regulation.bin is built per machine and co-op compares the file: a partner
    on a different libzstd would generate a regulation yours doesn't match, which
    shows up as a failure to connect rather than anything diagnosable.

    If it fires, the options are to match zstd versions across the party or to
    distribute one built file instead of building it on each machine.
    """
    import hashlib
    frame = dcx.write_zstd(_PINNED_PAYLOAD)[dcx.HEADER_SIZE:]
    assert hashlib.sha256(frame).hexdigest() == _PINNED_FRAME_SHA256
