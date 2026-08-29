from pathlib import Path

from . import gamebuild
from .gamebuild import GameBuildError
from .harden import is_hardened

_PROXY = ("dinput8.dll", "winhttp.dll")
_FORBIDDEN = _PROXY + ("modengine2.dll", "modengine.toml")
_SPAWNERS = ("ermerchant.dll", "erdtools.dll", "glorious_merchant.dll")


def scan_game_dir(game_dir):
    game_dir = Path(game_dir)
    found = []
    for name in _FORBIDDEN:
        if (game_dir / name).exists():
            found.append(name)
    if (game_dir / "mod" / "regulation.bin").exists():
        found.append("mod/regulation.bin")
    return found


def eac_state(game_dir):
    game_dir = Path(game_dir)
    if is_hardened(game_dir):
        # start_protected_game.exe exists here too (it's the eldenring copy),
        # so this check must come before the exe-presence check below, or a
        # hardened install would misreport as "armed".
        return "hardened"
    if not (game_dir / "start_protected_game.exe").exists():
        return "absent"
    if any((game_dir / p).exists() for p in _PROXY):
        return "disarmed"
    return "armed"


def _spawners_present(game_dir):
    game_dir = Path(game_dir)
    out = []
    # rglob("*.dll") is case-sensitive on Linux and would silently skip a
    # real Windows filename like "Glorious_Merchant.DLL" — walk everything
    # and compare the lowercased name instead.
    for child in game_dir.rglob("*"):
        if child.is_file() and child.name.lower() in _SPAWNERS:
            out.append(child.name)
    return out


def run_doctor(game_dir, report):
    game_dir = Path(game_dir)
    state = eac_state(game_dir)
    forbidden = scan_game_dir(game_dir)
    if state == "hardened":
        report.ok("EAC state: hardened (start_protected_game.exe swapped, EAC can't "
                   "fire — safe from accidental vanilla launch)")
    else:
        report.info(f"EAC state: {state}")
    if state in ("armed", "disarmed") and forbidden:
        # start_protected_game.exe still exists (and is the real EAC launcher),
        # so a vanilla online launch is still possible. A proxy DLL "disarms"
        # EAC's own clean-load check, but it does so by hooking the process via
        # the OS DLL search order BEFORE EAC's hooks activate — so "disarmed" is
        # not safer here, it's how the mod loads unnoticed. Treat armed and
        # disarmed the same: exe present + forbidden artifact present is the
        # dangerous mixed state. (hardened is exempt: start_protected_game.exe
        # is the eldenring copy there, so EAC can't fire regardless.)
        report.fail(f"start_protected_game.exe present (EAC {state}) AND mod artifacts present "
                    f"({', '.join(forbidden)}) — an online vanilla launch could still load a mod. "
                    "Remove them or use the seamless launch option.")
    elif forbidden:
        report.warn(f"mod artifacts present (EAC {state}): {', '.join(forbidden)}")
    else:
        report.ok("no proxy DLL / ModEngine artifacts in Game/")
    for sp in _spawners_present(game_dir):
        report.warn(f"item-spawner mod present: {sp} — never take its save to vanilla online")
    if (game_dir / "ersc_launcher.exe").exists():
        report.ok("Seamless Co-op launcher present")
    return report


def launcher_is_stale(game_dir):
    """(swapped, real) PE versions when the hardened swap is off the game build.

    Only meaningful on a hardened install, where start_protected_game.exe is a
    copy of eldenring.exe and the two are SUPPOSED to match. On an unhardened
    install it is the real EAC launcher, a different product with its own
    version, and comparing them would report a permanent false positive.

    Steam patches eldenring.exe without touching the swap -- and the swap is
    chattr +i, so it could not be replaced even if Steam tried.
    """
    game_dir = Path(game_dir)
    if not is_hardened(game_dir):
        return None
    try:
        swapped = gamebuild.read_exe_version(game_dir / "start_protected_game.exe")
        real = gamebuild.read_exe_version(game_dir / "eldenring.exe")
    except GameBuildError:
        return None
    return None if swapped == real else (swapped, real)


def run_build_checks(game_dir, stamped, live, report):
    """Report game-build drift. Offline: reads only local files."""
    report.info(f"game build: {live.app} (regulation {live.regulation}, "
                f"exe {live.exe}, steam buildid {live.steam_buildid})")
    if stamped is None:
        report.info("stack build: not recorded yet — `erm apply` will stamp it")
    else:
        changes = gamebuild.drift(stamped, live)
        if not changes:
            report.ok(f"stack was built for the installed build ({live.app})")
        else:
            report.warn(f"game build drift: stack built for {stamped.app}, "
                        f"game is {live.app} — run `erm apply`")
            if stamped.regulation != live.regulation:
                report.warn(f"merged regulation.bin targets {stamped.regulation}, "
                            f"game is {live.regulation}")
    stale = launcher_is_stale(game_dir)
    if stale:
        swapped, real = stale
        # A warning, not a failure: the swap still blocks EAC, it is just the
        # wrong build. Failing here would make doctor exit 1 on a safe install.
        report.warn(f"hardened launcher is stale: start_protected_game.exe {swapped}, "
                    f"eldenring.exe {real} — re-run `erm unharden && erm harden`")
    return report
