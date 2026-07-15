from pathlib import Path

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
