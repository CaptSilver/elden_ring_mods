import re
from pathlib import Path

from .paths import APPID


def _kv(text):
    return dict(re.findall(r'"([^"]+)"\s*"([^"]*)"', text))


def read_appmanifest(steam_root):
    acf = steam_root / "steamapps" / f"appmanifest_{APPID}.acf"
    data = {}
    if acf.exists():
        try:
            data = _kv(acf.read_text(errors="ignore"))
        except OSError:
            # Unreadable manifest (perms, TOCTOU race) — treat as absent
            # rather than leaking OSError to the CLI.
            data = {}
    size = int(data.get("SizeOnDisk", "0") or "0")
    data["installed"] = size > 0 and data.get("buildid", "0") != "0"
    return data


def cloud_saves(steam_root):
    out = []
    ud = steam_root / "userdata"
    if not ud.is_dir():
        return out
    try:
        accts = sorted(ud.iterdir())
    except OSError:
        # Unreadable userdata dir (perms, TOCTOU race) — return what we have
        # rather than leaking OSError to the CLI.
        return out
    for acct in accts:
        rc = acct / APPID / "remotecache.vdf"
        if not rc.exists():
            continue
        try:
            text = rc.read_text(errors="ignore")
        except OSError:
            # Unreadable remotecache.vdf (perms, TOCTOU race) — skip this
            # account rather than leaking OSError to the CLI.
            continue
        for m in re.finditer(r'"(EldenRing/(\d+)/[^"]+)"\s*\{[^}]*?"size"\s*"(\d+)"', text, re.S):
            out.append({
                "account_id": acct.name,
                "steamid64": m.group(2),
                "relpath": m.group(1),
                "size": int(m.group(3)),
            })
    return out


def steam_running():
    """True if a steam client process is up (best-effort, /proc scan)."""
    for pid in Path("/proc").glob("[0-9]*"):
        try:
            comm = (pid / "comm").read_text().strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if comm == "steam":
            return True
    return False
