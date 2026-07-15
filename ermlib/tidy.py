"""`erm tidy`: remove runtime cruft that `erm uninstall`/`switch` leave behind
and installed.json never tracked — per-mod `mods/<Name>/log.txt` dirs, ERSC
crash dumps, loader logs.

This is a DELETE command, so `find_cruft` is written to fail closed: a
candidate is returned only if it's provably associated with an erm-managed
mod AND passes every containment/recorded/critical check below. Anything
ambiguous (a non-runtime file in a mod dir, a symlink, a path that resolves
outside Game/) is left alone rather than guessed at.
"""
from pathlib import Path

RUNTIME_EXTS = {".txt", ".log", ".dmp", ".dat"}   # NOT .dll/.exe/.bin/.dcx/.ini/.so — those are content
RUNTIME_NAMES = {"metadata"}
CRITICAL = {"eldenring.exe", "start_protected_game.exe", "start_protected_game.exe.erm-backup"}
LOOSE_LOGS = {"mod_loader_log.txt", "anti_cheat_toggler_log.txt"}


def _is_runtime_file(p):
    return p.suffix.lower() in RUNTIME_EXTS or p.name in RUNTIME_NAMES


def _contained(game_res, p):
    """True iff p resolves to somewhere strictly inside game_res (blocks symlink escapes)."""
    try:
        p.resolve().relative_to(game_res)
        return True
    except (ValueError, OSError):
        return False


def find_cruft(game_dir, recorded):
    """Return a list of Paths safe to remove. `recorded` = set of installed.json-relative file paths
    (active mod files — never touched). Every returned path passed ALL safety checks below."""
    game = Path(game_dir)
    game_res = game.resolve()
    recorded_abs = set()
    for rel in recorded:
        try:
            recorded_abs.add((game / rel).resolve())
        except OSError:
            pass
    out = []

    def consider(p):
        # global gates every candidate must pass
        if p.name in CRITICAL:
            return
        if p.is_symlink():                       # never follow/act on a symlink
            return
        if not _contained(game_res, p):          # must stay inside Game/
            return
        if p.resolve() in recorded_abs:          # never an active recorded file
            return
        out.append(p)

    mods = game / "mods"
    if mods.is_dir():
        for sub in sorted(mods.iterdir()):
            if sub.is_symlink():
                continue
            if sub.is_dir():
                # a mod's runtime dir: mods/<Name>/ with NO mods/<Name>.dll installed,
                # and EVERY file inside is a runtime file, none recorded, all contained.
                if (mods / f"{sub.name}.dll").exists():
                    continue                     # mod still installed — leave its dir
                files = [f for f in sub.rglob("*") if f.is_file()]
                if files and not all(_is_runtime_file(f) for f in files):
                    continue                     # has a non-runtime file → skip whole dir
                if any(f.resolve() in recorded_abs for f in files):
                    continue
                if not all(_contained(game_res, f) for f in files):
                    continue
                consider(sub)
            elif sub.is_file() and (sub.name.endswith("_log.txt") or sub.suffix.lower() == ".log"):
                consider(sub)

    sc = game / "SeamlessCoop"
    if sc.is_dir() and not (sc / "ersc.dll").exists():   # ERSC uninstalled → its crash artifacts are orphaned
        for d in ("crashdumps", "crashpad"):
            consider(sc / d)

    for name in LOOSE_LOGS:
        consider(game / name)

    return out
