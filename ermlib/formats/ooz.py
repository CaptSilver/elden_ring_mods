"""Build and call the vendored Oodle Kraken decompressor.

FromSoftware ships game archives Kraken-compressed. There is no open-source
Kraken *encoder*, but we never need one: the game also reads plain zlib DCX
(see dcx.py), so decompress-only is enough.
"""
import ctypes
import hashlib
import subprocess
from pathlib import Path

from ..errors import ErmError

SRC_DIR = Path("vendor-src/ooz")
BUILD_DIR = Path("tools/ooz")
SOURCES = ("kraken_lib.cpp", "bitknit.cpp", "lzna.cpp")
# Bumped whenever the build flags change, so a flag-only edit still rebuilds.
BUILD_KEY = "1"

_lib = None


class OozError(ErmError):
    """The Kraken decompressor could not be built, loaded, or run."""


def _source_digest():
    """sha256 over the sources and build flags. Rebuild when this changes, so a
    vendored-source edit can't leave a stale .so behind."""
    h = hashlib.sha256(BUILD_KEY.encode())
    for name in sorted(SOURCES) + ["compat.h", "stdafx.h"]:
        h.update((SRC_DIR / name).read_bytes())
    return h.hexdigest()


def library_path():
    """Path to libooz.so, building it if missing or stale."""
    digest = _source_digest()
    stamp = BUILD_DIR / "build.sha256"
    lib = BUILD_DIR / "libooz.so"
    if lib.exists() and stamp.exists() and stamp.read_text().strip() == digest:
        return lib
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ["g++", "-O2", "-fPIC", "-shared", "-w",
           "-o", str(lib), *[str(SRC_DIR / s) for s in SOURCES]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise OozError(
            "g++ not found — it's needed once to build the Kraken decompressor "
            "that reads the game's compressed archives. It ships in the Bazzite "
            "base image; inside a distrobox run `dnf install gcc-c++`.") from exc
    if proc.returncode != 0:
        raise OozError(f"building libooz.so failed:\n{proc.stderr.strip()}")
    stamp.write_text(digest)
    return lib


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(library_path()))
        fn = lib.ooz_decompress_seekchunked
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                       ctypes.c_char_p, ctypes.c_size_t]
        _lib = fn
    return _lib


def decompress(src, uncompressed_size):
    """Kraken-decompress `src` to exactly `uncompressed_size` bytes.

    Raises rather than returning short output: a partial decode would produce a
    structurally valid but truncated archive, which nothing downstream catches.
    """
    fn = _load()
    # Slack: the decoder may overwrite past the logical end within a chunk.
    dst = ctypes.create_string_buffer(uncompressed_size + 65536)
    n = fn(src, len(src), dst, uncompressed_size)
    if n != uncompressed_size:
        raise OozError(
            f"Kraken decode produced {n} bytes, expected {uncompressed_size} "
            f"— the archive is truncated, or it isn't a Kraken stream")
    return dst.raw[:uncompressed_size]
