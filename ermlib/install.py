"""Install ERSC into Game/ and re-inject the co-op password.

An ERSC update ships its own ersc_settings.ini with cooppassword blank, so
apply() always has to rewrite it after extracting — otherwise the whole
group silently loses connection until someone notices.
"""
import re
import shutil
import zipfile
from pathlib import Path


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
    game_dir = Path(game_dir)
    legacy = game_dir / "launch_elden_ring_seamlesscoop.exe"
    if legacy.exists():
        legacy.unlink()
    sc = game_dir / "SeamlessCoop"
    if sc.exists():
        shutil.rmtree(sc)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(game_dir)
    inject_password(game_dir / "SeamlessCoop" / "ersc_settings.ini", password)
