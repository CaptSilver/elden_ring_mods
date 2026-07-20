"""The Steam launch-option commands erm knows how to print.

Every variant is built every time. What's installed on this machine only
annotates the output, so a box missing ReShade still shows you the line it
would use — you can copy a command for a machine you're not sitting at.
"""
import shlex
import shutil
from pathlib import Path

from .report import Report

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


_HEADER = ("Steam → ELDEN RING → Properties → Launch Options. "
           "Pick one line and paste it whole.")

_ME3_NOTE = """\
  The trailing `# %command%` is load-bearing. Steam runs this field through a shell and
  substitutes the game's Proton command line at %command%; the `#` comments that out so
  me3 runs alone. Drop the token and Steam appends your text as arguments to the game exe
  instead — vanilla boots with junk argv and no mods, silently. Both paths are absolute
  because Steam's working directory is Game/, not this repo, and ~/.local/bin is not on
  Steam's PATH."""

_ME3_MISSING = """\
  Install it before using this launcher; the ersc_launcher option below works without it."""

_ERSC_NOTE = """\
  This one rewrites the exe inside Steam's Proton line rather than replacing it. Keep the
  full filename in the pattern — a shorter substring rewrites every matching argument."""

_VALIDATOR_NOTE = """\
  Launches normally and dumps the real argv. The last token must be
  .../ELDEN RING/Game/start_protected_game.exe."""

_RESHADE_ON = """\
ReShade is installed on this machine, so the dxgi override variants apply here. They are
per-machine — on a box without ReShade (a Steam Deck) they point at a dxgi.dll that isn't
there. Use the plain variants over there."""

_RESHADE_OFF = """\
ReShade is not installed on this machine, so the plain variants are the ones to use here.
The dxgi override variants are listed for a box that does have it."""

_DUAL_GPU = """\
Dual GPU: prepend MESA_VK_DEVICE_SELECT=<vendor>:<device> (discover with
MESA_VK_DEVICE_SELECT=list %command%)."""


def _warn(msg):
    """A one-off warning, red on an interactive terminal like the rest of the CLI."""
    r = Report()
    r.warn(msg)
    return r.render()


def _pair(label, plain, reshade):
    return (f"{label}\n\n"
            f"  plain\n    {plain}\n\n"
            f"  ReShade\n    {reshade}\n")


def render(variants):
    """The full launch-option output as one string."""
    out = [_HEADER, ""]

    note = "me3 packages present" if variants["me3_packages"] else "no me3 packages"
    if variants["me3"] is None:
        out.append(f"me3 — loose-asset mods + Seamless (this install: {note})")
        out.append("")
        out.append(_warn("me3 is not installed on this machine "
                         "(not on PATH, not at ~/.local/bin/me3)."))
        out.append(_ME3_MISSING)
    else:
        if not variants["profile_exists"]:
            out.append(_warn(f"{PROFILE_DISPLAY} does not exist yet — "
                             "run `erm apply <profile>` first."))
        out.append(_pair(
            f"me3 — loose-asset mods + Seamless (this install: {note})",
            variants["me3"]["plain"], variants["me3"]["reshade"]))
        out.append(_ME3_NOTE)
    out.append("")

    out.append(_pair("ersc_launcher — Seamless only, no me3 packages",
                     variants["ersc"]["plain"], variants["ersc"]["reshade"]))
    out.append(_ERSC_NOTE)
    out.append("")

    out.append("validator — run once before trusting the ersc substitution")
    out.append("")
    out.append(f"    {variants['validator']}")
    out.append("")
    out.append(_VALIDATOR_NOTE)
    out.append("")

    out.append(_RESHADE_ON if variants["reshade_installed"] else _RESHADE_OFF)
    out.append("")
    out.append(_DUAL_GPU)
    return "\n".join(out)
