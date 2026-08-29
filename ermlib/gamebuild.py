"""What game build is installed, and what was this stack built for?

An Elden Ring patch moves several things at once, and which of them moved says
something different each time -- so a build identity is a tuple, not a number.

Two numbering schemes live in here and they are NOT the same. The PE version in
eldenring.exe is FromSoft's internal numbering (2.7.0.0 for the build the store
calls 1.17). The app version comes out of regulation.bin, where the mapping is
verified against every file we have. Don't cross them.
"""
import hashlib
import json
import struct
from pathlib import Path
from typing import NamedTuple

from .errors import ErmError
from .formats import regulation
from . import steam

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


# Decrypting the ~1.9 MB regulation in pure Python costs about five seconds,
# and `erm status` and `erm doctor` both want its build stamp on every run.
# Lives under tools/, already gitignored runtime state.
CACHE_PATH = Path("tools/build-stamps.json")


def _read_stamp_cache(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_stamp_cache(path, cache):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    except OSError:
        # A cache that can't be written costs the next run five seconds. It
        # cannot make an answer wrong, so it is not worth failing a command
        # that has everything it needs.
        pass


def cached_regulation_version(blob, cache_path=None):
    """read_regulation_version, memoised on the sha256 of the encrypted bytes.

    Hashing costs about a millisecond and changes the moment any byte of the
    file does, so a regulation that moved cannot read its old answer back --
    the cache is keyed by the thing being decoded, not by a path or an mtime.
    A cache that can't be read or written makes a run slow, never wrong.
    """
    key = hashlib.sha256(blob).hexdigest()
    path = Path(cache_path) if cache_path is not None else CACHE_PATH
    cache = _read_stamp_cache(path)
    hit = cache.get(key)
    if isinstance(hit, str):
        return hit
    version = read_regulation_version(blob)
    cache[key] = version
    _write_stamp_cache(path, cache)
    return version


class BuildId(NamedTuple):
    """Everything that identifies an installed build, from three sources.

    They are kept separate rather than reduced to one number because their
    disagreements are the signal -- see classify().
    """
    exe: str
    app: str
    regulation: str
    steam_buildid: str
    regulation_sha: str


class Change(NamedTuple):
    field: str
    was: str
    now: str


# What the difference between two BuildIds means.
UNCHANGED = "unchanged"     # nothing moved
PATCHED = "patched"         # a real game patch: exe/buildid AND regulation
REPACKAGED = "repackaged"   # depot update that left game data alone
TAMPERED = "tampered"       # regulation moved on its own -- nobody patched anything

_INSTALL_FIELDS = ("exe", "steam_buildid")
_DATA_FIELDS = ("regulation", "regulation_sha")


def identify(game_dir, steam_root):
    """The build currently installed at `game_dir`."""
    game_dir = Path(game_dir)
    reg_path = game_dir / "regulation.bin"
    try:
        blob = reg_path.read_bytes()
    except OSError as exc:
        raise GameBuildError(f"can't read {reg_path}: {exc}") from exc
    regver = cached_regulation_version(blob)
    manifest = steam.read_appmanifest(Path(steam_root))
    buildid = str(manifest.get("buildid", "") or "")
    # An absent or "0" buildid means Steam has no usable record of this install.
    # Defaulting it to "" would be worse than failing: an empty value compares
    # as a CHANGED one, which can flip a tampered install's classification to
    # "patched" and let a modified regulation.bin be adopted as vanilla.
    if not buildid or buildid == "0":
        raise GameBuildError(
            f"Steam has no build id for the game at {steam_root} — can't "
            "identify the installed build")
    return BuildId(
        exe=read_exe_version(game_dir / "eldenring.exe"),
        app=app_version(regver),
        regulation=regver,
        steam_buildid=buildid,
        regulation_sha=hashlib.sha256(blob).hexdigest(),
    )


def drift(stamped, live):
    """Which identity fields moved between `stamped` and `live`.

    An unstamped stack yields no drift: "unknown" is not "changed", and
    reporting five phantom changes on a first run would be noise.
    """
    if stamped is None:
        return ()
    return tuple(Change(f, getattr(stamped, f), getattr(live, f))
                 for f in BuildId._fields
                 if getattr(stamped, f) != getattr(live, f))


def classify(stamped, live):
    """What kind of change happened, from which fields moved together.

    A real patch moves the executable, the depot build and the game data at
    once. Game data moving *on its own* means nobody patched anything -- some
    other tool wrote to the install's regulation.bin. That distinction is the
    whole reason erm can trust Game/regulation.bin as a merge baseline without
    an external clean copy to compare against.
    """
    if stamped is None:
        return UNCHANGED
    install_moved = any(getattr(stamped, f) != getattr(live, f) for f in _INSTALL_FIELDS)
    data_moved = any(getattr(stamped, f) != getattr(live, f) for f in _DATA_FIELDS)
    if install_moved and data_moved:
        return PATCHED
    if install_moved:
        return REPACKAGED
    if data_moved:
        return TAMPERED
    return UNCHANGED
