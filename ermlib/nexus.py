"""Nexus Mods API client (Premium-only download endpoint).

Free accounts can list files but get HTTP 403 from download_link.json —
callers without a key should stick to the manual-download flow in cli.py
and never reach this module at all.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import __version__
from .errors import ErmError

API_BASE = "https://api.nexusmods.com/v1"
GAME = "eldenring"


def _headers(api_key):
    return {
        "apikey": api_key,
        "Application-Name": "erm",
        "Application-Version": __version__,
        "User-Agent": f"erm/{__version__} (+local)",
    }


def _urlopen(req):
    return urllib.request.urlopen(req, timeout=60)


def _api_get(path, api_key):
    req = urllib.request.Request(API_BASE + path, headers=_headers(api_key))
    try:
        with _urlopen(req) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ErmError("invalid Nexus API key") from exc
        if exc.code == 403:
            raise ErmError(
                "Nexus API download requires Premium (or the key lacks permission)"
            ) from exc
        if exc.code == 429:
            raise ErmError("Nexus API rate limit hit — wait and retry") from exc
        raise ErmError(f"Nexus API request failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ErmError(f"Nexus API unreachable: {exc}") from exc


def list_files(mod_id, api_key):
    return _api_get(f"/games/{GAME}/mods/{mod_id}/files.json", api_key)["files"]


def _version_key(file):
    # Parse "1.9.9" -> (1, 9, 9); non-numeric segments sort as 0 rather than
    # blowing up on a weird upstream version string. Ties (e.g. a re-upload
    # under the same version) break on upload time, newest wins.
    parts = []
    for p in (file.get("version") or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return (tuple(parts), file.get("uploaded_timestamp") or 0)


def main_files(files):
    # category_name == "MAIN" already excludes OLD_VERSION/ARCHIVED by
    # construction — Nexus only ever tags one category per file. Some mods
    # (e.g. Minimal HUD #148) ship several MAIN files at once — numbered
    # variants, not versions — so this can legitimately return more than one.
    return [f for f in files if f.get("category_name") == "MAIN"]


def pick_main_file(files):
    # Don't trust is_primary: it's False on the current ERSC #510 main file.
    # Only safe to auto-pick when there's exactly one MAIN file — callers with
    # several must disambiguate via file_id instead of guessing (see cli.py).
    candidates = main_files(files)
    if not candidates:
        raise ErmError("no MAIN file found in Nexus file list")
    return max(candidates, key=_version_key)


def find_file_by_version(files, version):
    for f in files:
        if f.get("category_name") == "MAIN" and f.get("version") == version:
            return f
    raise ErmError(f"version {version} not found on Nexus (removed?) — try --update")


def find_file_by_id(files, file_id):
    # Compared as text: the id arrives as an int from a profile and as a string
    # from the lockfile, and the two have to select the same file.
    for f in files:
        if str(f.get("file_id")) == str(file_id):
            return f
    raise ErmError(f"file id {file_id} not found on Nexus for this mod")


def download_url(mod_id, file_id, api_key):
    mirrors = _api_get(
        f"/games/{GAME}/mods/{mod_id}/files/{file_id}/download_link.json", api_key)
    if not mirrors:
        raise ErmError(f"no download mirrors returned for file {file_id}")
    chosen = next((m for m in mirrors if m.get("short_name") == "Nexus CDN"), mirrors[0])
    uri = chosen["URI"]
    # The URI has raw spaces in the filename portion of the path (e.g.
    # ".../Seamless Co-op v1.9.9-510-...zip?expires=..."), which urllib
    # rejects outright (InvalidURL). Percent-encode the path only — the query
    # carries expires/md5/user_id and must survive untouched.
    parts = urllib.parse.urlsplit(uri)
    path = urllib.parse.quote(parts.path, safe="/%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


# Nexus filenames come in two shapes:
#   legacy   Name-<modid>-<dashed version>-<epoch>.ext
#   current  Name <modid> <version> <ISO stamp> <token>.ext
# Both bury the variant name in front of machine noise.
_ARCHIVE_EXT = re.compile(r"\.(zip|7z|rar)$", re.I)
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z")
_EPOCH = re.compile(r"\b\d{9,}\b")
_TRAILING_TOKEN = re.compile(r"\s+[A-Za-z0-9]{9}\s*$")
_SEPARATORS = re.compile(r"[\s\-]+")
_VERSION_PARTS = re.compile(r"[.\-_]")


def variant_key(file_name, mod_id, version):
    """Which *variant* of a mod a file is, independent of its version.

    Normalise, THEN tokenise. Order is load-bearing: splitting first shatters
    an ISO stamp (2026-07-14T10-47Z) into 2026/07/14t10/47z, and those
    fragments then read as ordinary name words -- which is how an earlier cut
    of this matched none of map-for-goblins' nine variants instead of one.

    Version components are dropped as whole tokens, never as substrings: a
    mod whose version is "1" would otherwise lose every "1" in its name.
    """
    stem = _ARCHIVE_EXT.sub("", file_name)
    stem = _ISO_STAMP.sub("", stem)
    stem = _EPOCH.sub("", stem)
    stem = _TRAILING_TOKEN.sub("", stem)
    drop = {str(mod_id)}
    if version:
        v = str(version).lower()
        drop.update({v, v.lstrip("v")})
        drop.update(p for p in _VERSION_PARTS.split(v.lstrip("v")) if p)
    words = [t.lower() for t in _SEPARATORS.split(stem) if t]
    return " ".join(w for w in words
                    if w not in drop and w.lstrip("v") not in drop).strip()


class PinChoice(NamedTuple):
    """What to do with one mod's pin.

    `action` is "unchanged", "repin" or "ambiguous". `file` is the file to use
    and is None only when ambiguous; `candidates` is populated only then.
    """
    action: str
    file: dict
    candidates: tuple
    reason: str


def resolve_pin(current_file, files, mod_id, frozen=False):
    """Which file this mod should use now, never guessing between variants.

    The safety property: anything ambiguous keeps the current pin. The worst
    outcome is a missed update the report names, never a substituted mod --
    which is what makes moving pins automatically acceptable at all.
    """
    if frozen:
        if current_file is None:
            raise ErmError(
                f"mod {mod_id}: `freeze = true` needs a `file_id` — "
                "there is nothing to freeze")
        return PinChoice("unchanged", current_file, (), "frozen")

    mains = main_files(files)
    if not mains:
        return PinChoice("ambiguous", None, (), "no MAIN file on Nexus")

    if current_file is None:
        if len(mains) == 1:
            return PinChoice("unchanged", mains[0], (), "sole MAIN")
        return PinChoice("ambiguous", None, tuple(mains),
                         f"{len(mains)} MAIN files — set `file_id`")

    if any(m["file_id"] == current_file["file_id"] for m in mains):
        return PinChoice("unchanged", current_file, (), "pin is still a MAIN")

    if len(mains) == 1:
        return PinChoice("repin", mains[0], (), "sole MAIN supersedes the pin")

    key = variant_key(current_file["file_name"], mod_id, current_file.get("version"))
    matches = [m for m in mains
               if variant_key(m["file_name"], mod_id, m.get("version")) == key]
    if len(matches) == 1:
        return PinChoice("repin", matches[0], (), f"variant {key!r}")
    return PinChoice("ambiguous", None, tuple(mains),
                     f"variant {key!r} matched {len(matches)} of {len(mains)} MAIN files")
