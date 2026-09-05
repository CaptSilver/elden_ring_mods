import re
import subprocess
import zipfile
from pathlib import Path

from . import gamebuild, launch, manifest
from .conflicts import MERGED_ID
from .errors import ErmError
from .gamebuild import GameBuildError
from .harden import is_hardened

MERGED_REGULATION = "regulation.bin"

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


def merged_regulation_path(state):
    """Where the merged regulation.bin sits, per installed.json, or None.

    None means the stack composed no regulation at all, so there is no merged
    artifact whose build could have gone stale.
    """
    entry = (state or {}).get(MERGED_ID) or {}
    package = entry.get("package")
    if not package or MERGED_REGULATION not in (entry.get("paths") or {}):
        return None
    return Path(package) / MERGED_REGULATION


def _check_merged_regulation(state, live, report):
    """Compare the merged regulation on disk against the installed build.

    The build stamp records when `erm apply` last ran, not what the merge came
    out as -- apply re-stamps every run whether or not the merged file was
    rebuilt, so a fresh stamp can sit over game data from two patches ago.
    Only the file itself settles it.
    """
    path = merged_regulation_path(state)
    if path is None:
        return
    try:
        got = gamebuild.cached_regulation_version(path.read_bytes())
    except (OSError, GameBuildError) as exc:
        report.warn(f"can't read the build of merged regulation.bin ({exc}) — "
                    "run `erm apply` to rebuild it")
        return
    if got == live.regulation:
        report.ok(f"merged regulation.bin holds the installed build ({got})")
    else:
        report.warn(f"merged regulation.bin holds build {got}, game is "
                    f"{live.regulation} — the merged file carries older game "
                    f"data than the install")


def _declared_vanilla_regulations(profiles_base, report):
    """(mod id, member) for every regulation ancestor a profile declares.

    Doctor doesn't know which profile is applied and doesn't need to: two
    profiles declaring the same merged path have to agree or apply refuses
    them, so whichever declares it names the same ancestor. Deduped, so one
    declaration shared across the include graph is reported once.
    """
    out = []
    base = Path(profiles_base)
    for path in sorted(base.glob("*.toml")):
        try:
            prof = manifest.load_profile(path.stem, base=base)
        except (OSError, ErmError, ValueError) as exc:
            report.warn(f"can't read profile {path.name} ({exc})")
            continue
        for entry in prof.get("merges", []):
            source = entry.get("vanilla") or {}
            member = str(source.get("member", ""))
            if not member.lower().endswith(MERGED_REGULATION):
                continue
            pair = (source.get("mod"), member)
            if pair not in out:
                out.append(pair)
    return out


def _check_vanilla_ancestors(lock, live, report, profiles_base, vendor):
    """Warn when a merge's declared ancestor is for a different game build.

    Apply covers the gap by folding those merges onto the installed game's own
    regulation, so the output is not stuck on old game data -- but the mods
    were still built against that older build, and the fold only holds while
    every param they touch keeps the same row layout. That is what the layout
    gate decides on the next apply, and this is the condition it decides about.
    Nothing is said about an archive that isn't fetched -- there is no artifact
    on disk making a claim to check.
    """
    for mod_id, member in _declared_vanilla_regulations(profiles_base, report):
        asset = ((lock or {}).get(mod_id) or {}).get("asset")
        if not asset:
            continue
        archive = Path(vendor) / asset
        if not archive.exists():
            continue
        try:
            with zipfile.ZipFile(archive) as z:
                blob = z.read(member)
            got = gamebuild.cached_regulation_version(blob)
        except (OSError, KeyError, zipfile.BadZipFile, GameBuildError) as exc:
            report.warn(f"can't read the merge ancestor {member} in {asset} ({exc})")
            continue
        if got != live.regulation:
            report.warn(
                f"merge ancestor {member} in {asset} is build {got}, game is "
                f"{live.regulation} — apply rebases those merges onto the "
                f"game's own regulation, which holds only while the mods' "
                f"param layouts still fit this build")


def installed_me3_version():
    """The version the me3 on this machine reports.

    Three outcomes, and the caller needs to tell them apart: the version
    string, None when there is no binary at all, and "" when one is there but
    won't say — it can't be executed, it dies, or it prints something with no
    version in it. Collapsing that last case into None hides a launcher Steam
    will run and fail on.

    Asks the binary rather than reading installed.json: this one is installed
    outside the game and outside the repo, at the fixed path the Steam launch
    option names, so what is actually on disk is the only thing worth trusting.
    """
    binary = launch.ME3_FALLBACK
    if not binary.exists():
        return None
    try:
        out = subprocess.run([str(binary), "--version"], check=True,
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"(\d+\.\d+\.\d+)", out)
    return found.group(1) if found else ""


def _check_me3_host(lock, report):
    """Warn when the launcher Steam runs is behind the release erm pinned.

    Nothing else notices: the merged artifacts, the build stamp and the mod
    versions can all be correct while the loader that mounts them is older than
    the one the lockfile promises.
    """
    # Plain "me3" is a legacy pin: lockfiles written before the launcher got its
    # own entry named the same release under that key. Reading it keeps the
    # warning alive on the installs most likely to have drifted.
    pinned = ((lock or {}).get("me3-host") or (lock or {}).get("me3") or {}).get("version")
    if not pinned:
        return
    installed = installed_me3_version()
    if installed is None:
        return
    if not installed:
        # Warn, not fail: doctor exiting 1 would be wrong on an install that is
        # safe, just unlaunchable. Same reasoning as the stale-launcher warning.
        report.warn(f"me3 at {launch.ME3_FALLBACK} won't report a version — the "
                    f"launcher Steam runs may be broken; run `erm apply` to "
                    f"reinstall it")
        return
    if installed != pinned.lstrip("v"):
        report.warn(f"me3 launcher is stale: {launch.ME3_FALLBACK} reports {installed}, "
                    f"the lockfile pins {pinned} — run `erm apply` to update it")


def run_build_checks(game_dir, stamped, live, report, state=None, lock=None,
                     profiles_base=Path("profiles"), vendor=Path("vendor")):
    """Report build drift and any merged artifact built for another build.

    Offline: reads only local files.
    """
    report.info(f"game build: {live.app} (regulation {live.regulation}, "
                f"exe {live.exe}, steam buildid {live.steam_buildid})")
    if stamped is None:
        report.info("stack build: not recorded yet — `erm apply` will stamp it")
    else:
        changes = gamebuild.drift(stamped, live)
        if not changes:
            report.ok(f"stack was built for the installed build ({live.app})")
        else:
            # Name the fields that moved. A depot re-package leaves the app
            # version alone, and reporting only that reads "stack built for
            # 1.17.0, game is 1.17.0" -- a warning that argues with itself.
            moved = ", ".join(f"{c.field} {c.was} → {c.now}" for c in changes)
            report.warn(f"game build drift: {moved} — run `erm apply`")
    _check_merged_regulation(state, live, report)
    _check_vanilla_ancestors(lock, live, report, profiles_base, vendor)
    _check_me3_host(lock, report)
    stale = launcher_is_stale(game_dir)
    if stale:
        swapped, real = stale
        # A warning, not a failure: the swap still blocks EAC, it is just the
        # wrong build. Failing here would make doctor exit 1 on a safe install.
        report.warn(f"hardened launcher is stale: start_protected_game.exe {swapped}, "
                    f"eldenring.exe {real} — re-run `erm unharden && erm harden`")
    return report
