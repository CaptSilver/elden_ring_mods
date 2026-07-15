import hashlib
import json
import urllib.request

from .errors import IntegrityError

_UA = {"User-Agent": "erm/0.1 (+https://localhost)"}


def _fetch_bytes(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r:
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


def download_verified(url, dest, sha256):
    data = _fetch_bytes(url)
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise IntegrityError(f"sha256 mismatch for {url}: want {sha256}, got {got}")
    dest.write_bytes(data)
