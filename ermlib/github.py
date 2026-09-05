import hashlib
import json
import os
import urllib.request
from pathlib import Path

from . import USER_AGENT
from .errors import IntegrityError

_UA = {"User-Agent": USER_AGENT}
_CHUNK = 1 << 20


def _urlopen(url):
    req = urllib.request.Request(url, headers=_UA)
    return urllib.request.urlopen(req, timeout=60)


def _fetch_bytes(url):
    """Small bodies only — release JSON. Archives go through download_stream."""
    with _urlopen(url) as r:
        return r.read()


def _fetch_json(url):
    return json.loads(_fetch_bytes(url).decode())


def _release_from_json(data):
    assets = [{
        "name": a["name"],
        "url": a["browser_download_url"],
        "digest": a.get("digest"),
    } for a in data.get("assets", [])]
    return {"tag": data.get("tag_name"), "assets": assets}


def latest_release(repo_id):
    data = _fetch_json(f"https://api.github.com/repositories/{repo_id}/releases/latest")
    return _release_from_json(data)


def release_by_tag(repo_id, tag):
    data = _fetch_json(f"https://api.github.com/repositories/{repo_id}/releases/tags/{tag}")
    return _release_from_json(data)


def pick_asset(release, suffix=".zip", name_hint=None):
    candidates = [a for a in release["assets"] if a["name"].endswith(suffix)]
    if name_hint:
        # Some releases (me3) ship multiple .zip assets — a debug build and
        # the real one. "First .zip" would silently grab the wrong asset, so
        # prefer one whose name matches the hint; fall back to first .zip if
        # nothing matches rather than failing a fetch over a stale hint.
        hinted = [a for a in candidates if name_hint.lower() in a["name"].lower()]
        if hinted:
            return hinted[0]
    if candidates:
        return candidates[0]
    raise IntegrityError(f"no asset ending in {suffix}"
                          + (f" matching {name_hint!r}" if name_hint else "")
                          + f" in release {release.get('tag')}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_stream(url, dest, sha256=None):
    """Download to `dest`, hashing on the way past; returns the hexdigest.

    Streamed because the archives are the size they are: the pinned texture
    pack is 8 GB, and reading a body that size into one bytes object peaks
    around 12 GB of RSS and gets the process OOM-killed on any normal machine.

    Nothing lands on `dest` until the bytes are all there and the hash (when
    one is expected) matches, so a failed download can't leave a truncated
    archive that a later run would adopt as the real thing. Pass sha256=None
    for trust-on-first-use, where the digest we compute here IS the pin.
    """
    dest = Path(dest)
    # with_name, not with_suffix: with_suffix replaces everything after the
    # first dot, so me3-host-v0.13.0.tar.gz would write to a ".tar.part" that
    # collides with a sibling archive.
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with _urlopen(url) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        got = h.hexdigest()
        if sha256 is not None and got != sha256:
            raise IntegrityError(f"sha256 mismatch for {url}: want {sha256}, got {got}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return got


def download_verified(url, dest, sha256):
    """Fail-closed: an empty expected hash is a mismatch, not a skipped check."""
    return download_stream(url, dest, sha256=sha256)
