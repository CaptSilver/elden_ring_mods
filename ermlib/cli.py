import shutil
import time
from pathlib import Path

from . import paths, steam, manifest, github, install, saves
from .errors import PathError
from .report import Report
from .savefile import SaveFile
from .audit import audit_save
from .doctor import run_doctor

LAUNCH_OPTION = (
    "bash -c 'exec \"${@/start_protected_game.exe/ersc_launcher.exe}\"' -- %command%"
)
LAUNCH_VALIDATOR = (
    "bash -c 'printf \"%q\\n\" \"$@\" > /tmp/ercmd.txt; exec \"$@\"' -- %command%"
)


def cmd_launch_option(args):
    print("Steam → ELDEN RING → Properties → Launch Options:\n")
    print(f"  {LAUNCH_OPTION}\n")
    print("Validate once (last argv token must be .../Game/start_protected_game.exe):\n")
    print(f"  {LAUNCH_VALIDATOR}\n")
    print("Dual GPU: prepend MESA_VK_DEVICE_SELECT=<vendor>:<device> "
          "(discover with MESA_VK_DEVICE_SELECT=list %command%).")
    return 0


def cmd_audit(args):
    save_path = Path(args.save) if getattr(args, "save", None) else \
        paths.find_save_dir(paths.find_steam_root()) / "ER0000.sl2"
    try:
        data = save_path.read_bytes()
    except OSError as exc:
        raise PathError(f"cannot read save: {save_path} ({exc})") from exc
    sf = SaveFile.from_bytes(data)
    res = audit_save(sf)
    r = Report()
    if not res.findings:
        r.ok("no tampering signatures found")
    for f in res.findings:
        (r.fail if f.severity == "decisive" else r.warn)(f"[slot {f.slot}] {f.message}")
    print(r.render(as_json=args.json))
    print("\n" + res.caveat)
    return 0


def cmd_status(args):
    root = paths.find_steam_root()
    m = steam.read_appmanifest(root)
    r = Report()
    r.info(f"game installed: {m.get('installed')}  buildid={m.get('buildid')}")
    for cs in steam.cloud_saves(root):
        r.info(f"cloud save: account {cs['account_id']} {cs['relpath']} ({cs['size']} B)")
    print(r.render(as_json=args.json))
    return 0


def cmd_doctor(args):
    root = paths.find_steam_root()
    game = paths.find_game_dir(root)
    r = run_doctor(game, Report())
    print(r.render(as_json=args.json))
    return r.exit_code


def fetch_profile(profile_name, vendor, lock_path, profiles_base=Path("profiles")):
    prof = manifest.load_profile(profile_name, base=profiles_base)
    lock = manifest.load_lock(lock_path)
    vendor = Path(vendor)
    vendor.mkdir(exist_ok=True)
    for mod in prof["mods"]:
        if mod["source"] == "github":
            rel = github.latest_release(mod["repo_id"])
            asset = github.pick_asset(rel, suffix=".zip")
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            dest = vendor / f'{mod["id"]}-{rel["tag"]}.zip'
            github.download_verified(asset["url"], dest, digest)
            manifest.set_mod(lock, mod["id"], version=rel["tag"],
                             asset=dest.name, sha256=digest, source="github")
            print(f"✓ {mod['id']} {rel['tag']} verified → {dest.name}")
        else:
            nid = mod.get("nexus_id")
            print(f"! {mod['id']} is a manual Nexus download: "
                  f"https://www.nexusmods.com/eldenring/mods/{nid} "
                  f"— download the archive into {vendor}/ , then re-run apply.")
    manifest.write_lock(lock_path, lock)
    return lock


def cmd_fetch(args):
    fetch_profile(args.profile, Path("vendor"), Path("mods.lock.toml"))
    return 0


def cmd_apply(args):
    root = paths.find_steam_root()
    game = paths.find_game_dir(root)
    lock = manifest.load_lock("mods.lock.toml")
    ersc = lock.get("seamless-coop")
    if not ersc:
        print("no seamless-coop in lockfile — run `erm fetch` first")
        return 1
    password = install.read_secret(Path("secrets.env")) if Path("secrets.env").exists() else ""
    if not password:
        print("warning: no COOP_PASSWORD in secrets.env — password will be blank")
    install.apply_ersc(Path("vendor") / ersc["asset"], game, password)
    print(f"applied seamless-coop {ersc['version']} to {game}")
    return 0


def cmd_verify(args):
    lock = manifest.load_lock("mods.lock.toml")
    r = Report()
    for mod_id, meta in lock.items():
        p = Path("vendor") / meta.get("asset", "")
        if not p.exists():
            r.warn(f"{mod_id}: archive missing from vendor/")
            continue
        got = github.sha256_file(p)
        (r.ok if got == meta.get("sha256") else r.fail)(
            f"{mod_id}: {'sha256 ok' if got == meta.get('sha256') else 'HASH MISMATCH'}")
    print(r.render(as_json=args.json))
    return r.exit_code


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def cmd_backup(args):
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    co2 = list(save_dir.glob("*.co2")) or list(save_dir.glob("*.sl2"))
    if not co2:
        print("no save found to back up")
        return 1
    out = saves.backup_save(co2[0], Path("backups"), label=args.label or "", stamp=_stamp())
    print(f"backed up → {out}")
    return 0


def cmd_quarantine(args):
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    sl2 = save_dir / "ER0000.sl2"
    rep = saves.quarantine(sl2, Path("backups"), steam.cloud_saves(root),
                           steam_up=steam.steam_running(), stamp=_stamp())
    print(rep.render(as_json=args.json))
    return 0


def cmd_restore(args):
    src = Path(args.backup)
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    dest = save_dir / ("ER0000.co2" if src.name.endswith(".co2") or ".co2" in src.name else "ER0000.sl2")
    if dest.exists():
        saves.backup_save(dest, Path("backups"), label="pre-restore", stamp=_stamp())
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        raise PathError(f"cannot restore from {src} ({exc})") from exc
    print(f"restored {src} → {dest}")
    return 0


def register(subparsers):
    subparsers.add_parser("doctor", help="safety report").set_defaults(func=cmd_doctor)
    a = subparsers.add_parser("audit", help="forensic audit of a save")
    a.add_argument("save", nargs="?", help="path to ER0000.sl2 (default: live save)")
    a.set_defaults(func=cmd_audit)
    subparsers.add_parser("status", help="install + version summary").set_defaults(func=cmd_status)
    subparsers.add_parser("launch-option", help="print the Steam launch option").set_defaults(func=cmd_launch_option)
    f = subparsers.add_parser("fetch", help="download + verify a profile's mods")
    f.add_argument("profile", nargs="?", default="seamless-only")
    f.set_defaults(func=cmd_fetch)
    ap = subparsers.add_parser("apply", help="install the fetched mods into Game/")
    ap.add_argument("profile", nargs="?", default="seamless-only")
    ap.set_defaults(func=cmd_apply)
    subparsers.add_parser("verify", help="re-hash vendor/ against the lockfile").set_defaults(func=cmd_verify)
    b = subparsers.add_parser("backup", help="snapshot the co-op save")
    b.add_argument("--label", default="")
    b.set_defaults(func=cmd_backup)
    rs = subparsers.add_parser("restore", help="restore a save snapshot")
    rs.add_argument("backup")
    rs.set_defaults(func=cmd_restore)
    subparsers.add_parser("quarantine", help="move the vanilla save out of harm's way").set_defaults(func=cmd_quarantine)
