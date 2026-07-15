"""Install ERSC into Game/ and re-inject the co-op password.

An ERSC update ships its own ersc_settings.ini with cooppassword blank, so
apply() always has to rewrite it after extracting — otherwise the whole
group silently loses connection until someone notices.
"""
import re
import shutil
import zipfile
from pathlib import Path

from .errors import ErmError
from .paths import is_safe_relpath


def read_secret(env_path, key="COOP_PASSWORD"):
    for line in Path(env_path).read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def inject_password(settings_ini, password):
    ini = Path(settings_ini)
    text = ini.read_text()
    if "@COOP_PASSWORD@" in text:
        text = text.replace("@COOP_PASSWORD@", password)
    else:
        text = re.sub(r"(?m)^cooppassword\s*=.*$", f"cooppassword = {password}", text)
    ini.write_text(text)


def apply_ersc(zip_path, game_dir, password):
    """Extract the ERSC archive into game_dir and re-inject the password.

    Returns the list of real files it wrote (relative POSIX paths under
    game_dir, directories excluded) — `erm uninstall` records this so it
    knows exactly what to remove later.
    """
    game_dir = Path(game_dir)
    legacy = game_dir / "launch_elden_ring_seamlesscoop.exe"
    if legacy.exists():
        legacy.unlink()
    sc = game_dir / "SeamlessCoop"
    if sc.exists():
        shutil.rmtree(sc)
    with zipfile.ZipFile(zip_path) as z:
        # Zip-slip guard: a trojaned archive could name a member
        # ../../../etc/x and have extractall write outside game_dir. The
        # sha256 pin proves the archive is the chosen one, not that it's
        # benign. Reject any absolute path or one with a `..` component
        # BEFORE extracting anything, so a bad archive is never partially
        # written. The returned list is then guaranteed safe relative paths.
        for name in z.namelist():
            if not is_safe_relpath(name):
                raise ErmError(f"unsafe path in mod archive (refusing to install): {name}")
        z.extractall(game_dir)
        files = [n for n in z.namelist() if not n.endswith("/")]
    inject_password(game_dir / "SeamlessCoop" / "ersc_settings.ini", password)
    return files
