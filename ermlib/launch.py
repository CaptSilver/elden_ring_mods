"""The Steam launch-option commands erm knows how to print.

Every variant is built every time. What's installed on this machine only
annotates the output, so a box missing ReShade still shows you the line it
would use — you can copy a command for a machine you're not sitting at.
"""
import shlex
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = REPO_ROOT / "tools" / "me3" / "erm-coop.me3"
# How the profile is spelled in prose — the absolute form is for the command line.
PROFILE_DISPLAY = "tools/me3/erm-coop.me3"
ME3_FALLBACK = Path("~/.local/bin/me3").expanduser()

LAUNCH_OPTION = (
    "bash -c 'exec \"${@/start_protected_game.exe/ersc_launcher.exe}\"' -- %command%"
)
LAUNCH_VALIDATOR = (
    "bash -c 'printf \"%q\\n\" \"$@\" > /tmp/ercmd.txt; exec \"$@\"' -- %command%"
)
# ReShade (installed per-machine via reshade-steam-proton) chainloads as dxgi.dll
# and needs this env prefix.
RESHADE_ENV = 'WINEDLLOVERRIDES="d3dcompiler_47=n;dxgi=n,b" '


def find_me3():
    """Absolute path to the me3 binary, or None if it isn't installed.

    Steam's launch options don't inherit a login PATH, so a bare `me3` exits 127.
    """
    found = shutil.which("me3")
    if found:
        return Path(found).resolve()
    if ME3_FALLBACK.exists():
        return ME3_FALLBACK.resolve()
    return None


def me3_command(me3_bin, profile=None):
    """me3's launch line for Steam's Launch Options field.

    `# %command%` comments out the Proton chain Steam substitutes there, so me3
    runs alone and builds its own. The token has to be present: without it Steam
    appends this text as arguments to the game exe, and vanilla boots with junk
    argv, no mods, and no error.

    `profile=None` rather than `profile=PROFILE`: a default binds at def time, so
    the constant form would ignore any later reassignment of PROFILE — which is
    how the tests point this at a scratch dir.
    """
    profile = PROFILE if profile is None else profile
    return (f"{shlex.quote(str(me3_bin))} launch "
            f"-p {shlex.quote(str(profile))} # %command%")


def build_variants(me3_bin, reshade_installed, me3_packages, profile=None):
    """Every launch command, plus the machine observations that annotate them.

    The observations are outputs, not inputs to a decision — the commands are
    identical whatever they say.
    """
    profile = PROFILE if profile is None else profile
    me3 = None
    if me3_bin is not None:
        plain = me3_command(me3_bin, profile)
        me3 = {"plain": plain, "reshade": RESHADE_ENV + plain}
    return {
        "me3": me3,
        "ersc": {"plain": LAUNCH_OPTION, "reshade": RESHADE_ENV + LAUNCH_OPTION},
        "validator": LAUNCH_VALIDATOR,
        "reshade_installed": bool(reshade_installed),
        "me3_packages": bool(me3_packages),
        "profile_exists": Path(profile).exists(),
    }
