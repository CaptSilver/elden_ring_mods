"""Rebuild the stack against the game build that is actually installed.

The merge engine is already a rebase engine: param-rows transplants a mod's
authored rows onto a base file. Swap the base for the patched game's own
regulation.bin and every regulation mod forward-ports by itself -- provided no
param's row layout moved, which is what the layout gate certifies.

Planning is separated from execution so a dry run costs nothing and the risky
part is testable without a filesystem. This is the code that can quietly
corrupt a regulation; it does not get to hide inside apply.
"""
from pathlib import Path

from .errors import ErmError

# Lives under tools/, which is already gitignored runtime state -- these are
# derived artifacts, not something a fresh clone should carry.
BASELINE_DIR = Path("tools/baselines")


class HealError(ErmError):
    """A heal could not be planned or completed."""


def baseline_path(regver, base=BASELINE_DIR):
    return Path(base) / f"regulation-{regver}.bin"


def adopt_baseline(game_dir, live, base=BASELINE_DIR):
    """Take the installed game's regulation.bin as the vanilla merge baseline.

    Keyed by build version and never overwritten. Every historical baseline is
    kept so an old merge can be reproduced, and so a tampered install has
    something known-good to fall back to.
    """
    dest = baseline_path(live.regulation, base)
    if dest.exists():
        return dest
    src = Path(game_dir) / "regulation.bin"
    try:
        blob = src.read_bytes()
    except OSError as exc:
        raise HealError(f"can't read {src}: {exc}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return dest
