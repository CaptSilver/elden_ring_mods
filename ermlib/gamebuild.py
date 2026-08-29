"""What game build is installed, and what was this stack built for?

An Elden Ring patch moves several things at once, and which of them moved says
something different each time -- so a build identity is a tuple, not a number.

Two numbering schemes live in here and they are NOT the same. The PE version in
eldenring.exe is FromSoft's internal numbering (2.7.0.0 for the build the store
calls 1.17). The app version comes out of regulation.bin, where the mapping is
verified against every file we have. Don't cross them.
"""
import hashlib
import struct
from pathlib import Path
from typing import NamedTuple

from .errors import ErmError
from .formats import regulation

# VS_FIXEDFILEINFO's signature. Searching for it beats walking the PE resource
# directory: the struct is fixed-layout, and an exe carries exactly one.
_FIXEDFILEINFO_SIG = struct.pack("<I", 0xFEEF04BD)
_VERSION_DWORDS_AT = 8          # past signature + struct version
_REG_VERSION_AT = 0x18
_REG_VERSION_LEN = 8
# Constant tail on every regulation version seen: 11601000, 11611000, 11701000.
_REG_VERSION_TAIL = "1000"


class GameBuildError(ErmError):
    """A game build could not be identified.

    Always raised, never degraded to a None return: a build we can't read must
    stop the run, because the alternative is reporting "no drift" for a stack
    that has silently gone stale.
    """


def read_exe_version(path):
    """The PE file version of `path`, e.g. "2.7.0.0"."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise GameBuildError(f"can't read {path}: {exc}") from exc
    at = data.find(_FIXEDFILEINFO_SIG)
    if at < 0:
        raise GameBuildError(f"{path} carries no version resource")
    fields_at = at + _VERSION_DWORDS_AT
    if fields_at + 8 > len(data):
        raise GameBuildError(f"{path} version resource is truncated")
    ms, ls = struct.unpack_from("<II", data, fields_at)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def app_version(regver):
    """"11701000" -> "1.17.0": major, two-digit minor, patch, constant tail."""
    if (len(regver) != 8 or not regver.isdigit()
            or not regver.endswith(_REG_VERSION_TAIL)):
        raise GameBuildError(
            f"unrecognised regulation version {regver!r} — expected 8 digits "
            f"ending {_REG_VERSION_TAIL}")
    return f"{regver[0]}.{int(regver[1:3])}.{regver[3]}"


def read_regulation_version(blob):
    """The 8-byte ASCII build stamp in a regulation.bin's BND4 header."""
    try:
        payload = regulation.unpack(blob)
    except (ErmError, ValueError, struct.error) as exc:
        raise GameBuildError(f"can't read regulation.bin: {exc}") from exc
    raw = payload[_REG_VERSION_AT:_REG_VERSION_AT + _REG_VERSION_LEN]
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GameBuildError(
            f"regulation version field is not ASCII: {raw!r}") from exc
