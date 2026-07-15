import shutil
from pathlib import Path

from .errors import SafetyError
from .report import Report


def backup_save(save_path, backups_dir, label="", stamp=None):
    save_path = Path(save_path)
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{label}" if label else ""
    dest = backups_dir / f"{save_path.name}.{stamp}{suffix}"
    shutil.copy2(save_path, dest)
    return dest


def list_backups(backups_dir):
    backups_dir = Path(backups_dir)
    if not backups_dir.exists():
        return []
    return sorted(p for p in backups_dir.iterdir() if p.is_file())


def quarantine(save_path, backups_dir, cloud_saves, steam_up, stamp=None):
    if steam_up:
        raise SafetyError("close Steam before quarantine (it may re-sync the save)")
    save_path = Path(save_path)
    backups_dir = Path(backups_dir)
    rep = Report()
    if save_path.exists():
        backup_save(save_path, backups_dir / "quarantine-backup", label="orig", stamp=stamp)
        qdir = backups_dir / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(save_path), str(qdir / f"{save_path.name}.{stamp}"))
        rep.ok(f"moved {save_path.name} out of the prefix into {qdir}")
    else:
        rep.warn(f"{save_path} not present (already quarantined?)")
    for cs in cloud_saves:
        rep.warn(f"purge Steam Cloud: account {cs['account_id']} — in Steam, disable Cloud for "
                 f"Elden Ring, then delete the remote {cs['relpath']} (View → Settings → Cloud → "
                 "Manage), or it will re-sync.")
    return rep
