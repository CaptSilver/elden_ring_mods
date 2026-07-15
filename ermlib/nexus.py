"""Nexus Mods API client (Premium-only download endpoint).

Free accounts can list files but get HTTP 403 from download_link.json —
callers without a key should stick to the manual-download flow in cli.py
and never reach this module at all.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

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


def pick_main_file(files):
    # category_name == "MAIN" already excludes OLD_VERSION/ARCHIVED by
    # construction — Nexus only ever tags one category per file. Don't trust
    # is_primary: it's False on the current ERSC #510 main file.
    candidates = [f for f in files if f.get("category_name") == "MAIN"]
    if not candidates:
        raise ErmError("no MAIN file found in Nexus file list")
    return max(candidates, key=_version_key)


def find_file_by_version(files, version):
    for f in files:
        if f.get("category_name") == "MAIN" and f.get("version") == version:
            return f
    raise ErmError(f"version {version} not found on Nexus (removed?) — try --update")


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
