"""Regenerate the erm-owned me3 profile (erm-coop.me3) from install state.

Derive, don't mutate: the profile is a pure function of installed.json, so any
order of apply/uninstall that reaches the same state yields identical output.
"""
from pathlib import Path

from . import state as state_mod

USER_MARKER = "# === user additions (preserved below) ==="
_USER_DEFAULT = (
    USER_MARKER + "\n"
    "# Anything below this line is kept across erm regenerations. Add your own\n"
    "# native or package entries here (e.g. a generated randomizer regulation.bin).\n"
)


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _user_region(profile):
    """The preserved tail of an existing profile (from USER_MARKER on), or the default."""
    if profile.exists():
        text = profile.read_text()
        idx = text.find(USER_MARKER)
        if idx != -1:
            return text[idx:].rstrip("\n") + "\n"
    return _USER_DEFAULT


def reconcile(state, me3_dir, game_dir):
    me3_dir = Path(me3_dir)
    profile = me3_dir / "erm-coop.me3"
    lines = ['profileVersion = "v1"', "", "[[supports]]", 'game = "eldenring"', ""]
    if "seamless-coop" in state:
        ersc = (Path(game_dir) / "SeamlessCoop" / "ersc.dll").resolve()
        lines += ["[[natives]]", f"path = '{ersc}'", ""]
    for mid, _package in state_mod.me3_packages(state):
        lines += ["[[packages]]", f'id = "{_esc(mid)}"', f"path = 'mods/{mid}/'", ""]
    me3_dir.mkdir(parents=True, exist_ok=True)
    user = _user_region(profile)
    profile.write_text("\n".join(lines) + user)
    return profile
