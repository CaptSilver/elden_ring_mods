import shutil
import time
import urllib.error
import zipfile
from pathlib import Path

from . import paths, steam, manifest, github, install, saves, nexus, harden
from . import state as state_mod
from .errors import NetworkError, PathError
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


def _default_nexus_api_key():
    secrets_path = Path("secrets.env")
    if not secrets_path.exists():
        return ""
    return install.read_secret(secrets_path, "NEXUS_API_KEY")


def fetch_profile(profile_name, vendor, lock_path, profiles_base=Path("profiles"),
                   update=False, nexus_api_key=None):
    try:
        prof = manifest.load_profile(profile_name, base=profiles_base)
    except OSError as exc:
        raise PathError(f"unknown profile '{profile_name}': {exc}") from exc
    lock = manifest.load_lock(lock_path)
    vendor = Path(vendor)
    vendor.mkdir(exist_ok=True)
    if nexus_api_key is None:
        nexus_api_key = _default_nexus_api_key()
    for mod in prof["mods"]:
        if mod["source"] == "github":
            locked = lock.get(mod["id"])
            pinned = not update and locked and locked.get("version")
            try:
                if pinned:
                    # Reproducibility promise: everyone who clones the repo and
                    # runs `erm fetch` gets THIS exact release, not whatever's
                    # newest. Verify against the sha we already trust — if
                    # upstream mutated the tagged asset, fail closed.
                    rel = github.release_by_tag(mod["repo_id"], locked["version"])
                    asset = github.pick_asset(rel, suffix=".zip",
                                               name_hint=mod.get("asset_match"))
                    digest = locked.get("sha256") or ""
                else:
                    rel = github.latest_release(mod["repo_id"])
                    asset = github.pick_asset(rel, suffix=".zip",
                                               name_hint=mod.get("asset_match"))
                    digest = (asset.get("digest") or "").removeprefix("sha256:")
                dest = vendor / f'{mod["id"]}-{rel["tag"]}.zip'
                github.download_verified(asset["url"], dest, digest)
            except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
                raise NetworkError(f"failed to fetch {mod['id']} from GitHub: {exc}") from exc
            manifest.set_mod(lock, mod["id"], version=rel["tag"],
                             asset=dest.name, sha256=digest, source="github")
            note = " (pinned)" if pinned else ""
            print(f"✓ {mod['id']} {rel['tag']}{note} verified → {dest.name}")
        elif mod["source"] == "nexus":
            nid = mod.get("nexus_id")
            if not nexus_api_key:
                # Free accounts can't hit the download_link.json endpoint —
                # same manual-download instruction as always.
                print(f"! {mod['id']} is a manual Nexus download: "
                      f"https://www.nexusmods.com/eldenring/mods/{nid} "
                      f"— download the archive into {vendor}/ , then re-run apply.")
                continue
            locked = lock.get(mod["id"])
            pinned = not update and locked and locked.get("version")
            try:
                if pinned:
                    # Same reproducibility promise as the GitHub pin: verify
                    # against the LOCKED sha256, not anything Nexus reports —
                    # Nexus's files.json carries no hash to trust anyway.
                    files = nexus.list_files(nid, nexus_api_key)
                    f = nexus.find_file_by_version(files, locked["version"])
                    url = nexus.download_url(nid, f["file_id"], nexus_api_key)
                    dest = vendor / f["file_name"]
                    github.download_verified(url, dest, locked["sha256"])
                    digest = locked["sha256"]
                else:
                    # No pin and no upstream hash to check against: trust on
                    # first use — download, then hash what actually landed on
                    # disk and pin THAT. Every later fetch (yours or a
                    # friend's, via the shared lockfile) verifies against it.
                    files = nexus.list_files(nid, nexus_api_key)
                    f = nexus.pick_main_file(files)
                    url = nexus.download_url(nid, f["file_id"], nexus_api_key)
                    dest = vendor / f["file_name"]
                    dest.write_bytes(github._fetch_bytes(url))
                    digest = github.sha256_file(dest)
            except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
                raise NetworkError(f"failed to fetch {mod['id']} from Nexus: {exc}") from exc
            manifest.set_mod(lock, mod["id"], version=f["version"],
                             asset=f["file_name"], sha256=digest, source="nexus")
            verb = "(pinned) verified" if pinned else "fetched"
            print(f"✓ {mod['id']} v{f['version']} {verb} → {f['file_name']}")
        else:
            raise PathError(f"unknown source '{mod['source']}' for mod '{mod['id']}'")
    manifest.write_lock(lock_path, lock)
    return lock


def cmd_fetch(args):
    fetch_profile(args.profile, Path("vendor"), Path("mods.lock.toml"), update=args.update)
    return 0


def _install_ersc(game, lock):
    """Install the locked seamless-coop archive into game/ and re-inject the password.
    Returns (version, had_password). Raises PathError if it can't (not fetched)."""
    ersc = lock.get("seamless-coop")
    if not ersc:
        raise PathError("no seamless-coop in lockfile — run `erm fetch` first")
    asset = ersc.get("asset")
    if not asset:
        raise PathError("seamless-coop lock entry has no asset recorded — run `erm fetch` first")
    vendor_path = Path("vendor") / asset
    if not vendor_path.exists():
        raise PathError(f"run `erm fetch` first — vendor archive missing: {vendor_path}")
    password = install.read_secret(Path("secrets.env")) if Path("secrets.env").exists() else ""
    files = install.apply_ersc(vendor_path, game, password)
    version = ersc.get("version", "?")
    state = state_mod.load_state()
    state_mod.record_install(state, "seamless-coop", version, asset, files)
    state_mod.write_state(Path("installed.json"), state)
    return version, bool(password)


_MANUAL_NOTES = {
    "item-enemy-randomizer": "run the randomizer generator (in the vendor archive) to create a "
                              "regulation.bin, then place it per its README; the whole group needs "
                              "the identical output",
    "me3": "me3 is a loader — install per me3.help; it chainloads ersc.dll and the randomizer",
}


def cmd_apply(args):
    """Install every auto-installable mod in a profile into Game/ (or Game/mods/,
    per each mod's `install` field) and record what landed where in installed.json.

    The default profile is seamless-only, so a bare `erm apply` still just
    installs seamless-coop — same as before this generalized to profiles.
    """
    steam_root = paths.find_steam_root()
    game = paths.find_game_dir(steam_root)
    try:
        profile = manifest.load_profile(args.profile)
    except OSError as exc:
        raise PathError(f"unknown profile '{args.profile}': {exc}") from exc
    lock = manifest.load_lock("mods.lock.toml")
    state = state_mod.load_state()
    password = install.read_secret(Path("secrets.env")) if Path("secrets.env").exists() else ""
    r = Report()
    installed_seamless = False
    for mod in profile["mods"]:
        mid = mod["id"]
        kind = mod.get("install", "game")
        if kind == "manual":
            r.info(f"{mid}: manual — {_MANUAL_NOTES.get(mid, 'see the mod README')}")
            continue
        meta = lock.get(mid)
        asset = meta.get("asset") if meta else None
        if not asset:
            r.warn(f"{mid}: not fetched — run `erm fetch {args.profile}` first")
            continue
        vpath = Path("vendor") / asset
        if not vpath.exists():
            r.warn(f"{mid}: archive missing from vendor/ ({asset})")
            continue
        # randomizer/me3 are tools, not Game/ mods: extract to tools/<mid>/
        # instead of Game/ and never touch installed.json — `erm uninstall`
        # only knows how to clean up files it put inside the game dir.
        if kind == "randomizer":
            try:
                install.extract_archive(vpath, Path("tools"), mid)
            except (OSError, zipfile.BadZipFile) as exc:
                r.warn(f"{mid}: extract failed ({exc})")
                continue
            r.ok(f"{mid}: generator extracted to tools/{mid}/")
            exe = (Path("tools") / mid / "randomizer" / "EldenRingRandomizer.exe").resolve()
            proton = paths.find_proton()
            try:
                compatdata = paths.find_compatdata(steam_root)
            except PathError:
                compatdata = None
            if proton and compatdata:
                r.info(
                    "generate regulation.bin (run the generator under Proton):\n"
                    f'  STEAM_COMPAT_DATA_PATH="{compatdata}" '
                    f'STEAM_COMPAT_CLIENT_INSTALL_PATH="{steam_root}" "{proton}" run "{exe}"'
                )
            else:
                r.info(f"generate regulation.bin by running {exe} under Proton/Wine "
                        "(couldn't auto-fill the Proton run command — no Proton found "
                        "or no compat data yet)")
            r.info("pick options + a seed, generate, then load the output via me3; "
                    "share the identical output with your group")
            continue
        if kind == "me3":
            try:
                install.extract_archive(vpath, Path("tools"), mid)
            except (OSError, zipfile.BadZipFile) as exc:
                r.warn(f"{mid}: extract failed ({exc})")
                continue
            me3dir = Path("tools") / mid
            prof = me3dir / "erm-coop.me3"
            if not prof.exists():
                prof.write_text(
                    'profileVersion = "v1"\n\n[[supports]]\ngame = "eldenring"\n\n'
                    '# Chainload Seamless Co-op — point at your installed ersc.dll:\n'
                    '# [[native]]\n# path = "/abs/path/to/Game/SeamlessCoop/ersc.dll"\n\n'
                    '# Load the randomizer output — point at the generated mod folder:\n'
                    '# [[package]]\n# id = "randomizer"\n# path = "/abs/path/to/generated/mod"\n'
                )
            r.ok(f"{mid}: extracted to tools/{mid}/ (loader — replaces the Steam launch-option method)")
            r.info(f"edit the starter profile {prof} to point at your ersc.dll + randomizer output, "
                    "then launch via me3 (Linux setup differs — see https://me3.help)")
            continue
        subdir = "mods" if kind == "mods" else ""
        # A corrupt/truncated archive (BadZipFile) or an I/O error on one mod
        # must not sink the whole run — warn, skip it, and keep going so the
        # mods already installed this pass still get recorded by write_state
        # below. ErmError (the zip-slip refusal) is intentionally NOT caught:
        # a hostile-path archive should still abort loudly.
        try:
            if mid == "seamless-coop":
                files = install.apply_ersc(vpath, game, password)   # legacy-clean + password inject
            else:
                files = install.extract_archive(vpath, game, subdir)
        except (OSError, zipfile.BadZipFile) as exc:
            r.warn(f"{mid}: install failed ({exc})")
            continue
        if mid == "seamless-coop":
            installed_seamless = True
        state_mod.record_install(state, mid, meta.get("version", "?"), asset, files)
        r.ok(f"{mid} {meta.get('version', '')} → {subdir or 'Game/'}")
    state_mod.write_state(Path("installed.json"), state)
    if installed_seamless and not password:
        r.warn("no COOP_PASSWORD in secrets.env — password left blank")
    print(r.render(as_json=args.json))
    print("\nSafety check (erm doctor):")
    dr = run_doctor(game, Report())
    print(dr.render(as_json=args.json))
    return dr.exit_code


def cmd_update(args):
    lock_path = Path("mods.lock.toml")
    before = {k: v.get("version") for k, v in manifest.load_lock(lock_path).items()}
    fetch_profile(args.profile, Path("vendor"), lock_path, update=True)
    after = manifest.load_lock(lock_path)

    r = Report()
    changed = []
    for mod_id, meta in after.items():
        old, new = before.get(mod_id), meta.get("version")
        if old != new:
            r.ok(f"{mod_id} {old or '(new)'} -> {new}")
            changed.append(mod_id)
        else:
            r.info(f"{mod_id} already latest ({new})")

    installed_version = None
    doctor_report = None
    if "seamless-coop" in changed:
        game = paths.find_game_dir(paths.find_steam_root())
        installed_version, had_password = _install_ersc(game, after)
        if not had_password:
            r.warn("no COOP_PASSWORD in secrets.env — password left blank")
        doctor_report = run_doctor(game, Report())

    print(r.render(as_json=args.json))
    if changed:
        if installed_version:
            print(f"\nInstalled seamless-coop {installed_version} into the game.")
        print("LOCKSTEP: every player must update to the same version and use the shared "
              "mods.lock.toml, or co-op won't connect. Commit and share the updated lockfile.")
        if doctor_report is not None:
            print("\nSafety check (erm doctor):")
            print(doctor_report.render(as_json=args.json))
    else:
        print("\nAlready up to date — nothing to install.")
    return doctor_report.exit_code if doctor_report is not None else 0


def _uninstall_one(game, mod_id, state, r):
    """Remove a single mod's files from game, appending progress to r and
    forgetting it in state. Shared by single-mod uninstall, profile uninstall,
    and switch, so every caller gets the same guards. Raises PathError if
    there's nothing safe to derive the file list from (never touches disk
    in that case)."""
    entry = state.get(mod_id)
    if entry and entry.get("files"):
        files = entry["files"]
        source = "install manifest"
    else:
        # No record of what was installed (manifest predates this feature, or
        # was deleted) — fall back to the vendor archive's own file list. If
        # neither exists there's nothing safe to remove.
        lock = manifest.load_lock("mods.lock.toml")
        meta = lock.get(mod_id, {})
        asset = meta.get("asset")
        vpath = Path("vendor") / asset if asset else None
        if not (vpath and vpath.exists()):
            raise PathError(
                f"nothing recorded for {mod_id} and no vendor archive to derive from — nothing to uninstall")
        try:
            with zipfile.ZipFile(vpath) as z:
                files = [n for n in z.namelist() if not n.endswith("/")]
        except (OSError, zipfile.BadZipFile) as exc:
            raise PathError(f"cannot read vendor archive {asset}: {exc}") from exc
        source = f"vendor archive {asset}"
    # Zip-slip guard, second layer: apply_ersc/extract_archive already refuse
    # unsafe archives at install, but this list can also come from the
    # fallback archive or a hand-edited installed.json — so re-validate
    # before deleting anything. Reject absolute/`..` paths, and confirm the
    # resolved path stays under the game dir. Resolve only for that
    # containment check — then act on the LITERAL game/rel, never the
    # resolved target. A recorded symlink pointing at a stock file (e.g. ->
    # eldenring.exe) would otherwise resolve inside Game/, pass containment,
    # and get its TARGET deleted; unlinking the literal removes the symlink
    # itself and leaves eldenring.exe intact. A refused entry warns but
    # doesn't abort the rest.
    game_resolved = game.resolve()
    safe = []
    for rel in files:
        if not paths.is_safe_relpath(rel):
            r.warn(f"refusing unsafe path: {rel}")
            continue
        literal = game / rel
        try:
            literal.resolve().relative_to(game_resolved)
        except ValueError:
            r.warn(f"refusing path outside game dir: {rel}")
            continue
        safe.append(literal)
    removed = 0
    for literal in safe:
        try:
            if literal.is_symlink() or literal.is_file():
                literal.unlink()
                removed += 1
        except OSError as exc:
            r.warn(f"could not remove {literal.name}: {exc}")
    # Prune now-empty dirs these files lived in, deepest first, so a parent
    # dir left empty by its last child doesn't linger — but never the game
    # root itself, and never a dir that still has something in it. Drive this
    # off the validated literals only, never the raw list.
    for d in sorted({literal.parent for literal in safe}, key=lambda x: len(str(x)), reverse=True):
        try:
            if d != game and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    state_mod.forget(state, mod_id)
    r.ok(f"removed {removed} file(s) for {mod_id} (from {source})")
    return removed


def cmd_uninstall(args):
    """Remove a single mod's files, or every mod in a profile if args.mod
    names one (profiles/<args.mod>.toml exists). Manual-install mods in a
    profile are skipped — erm never extracted them, so there's nothing of
    its own to clean up."""
    game = paths.find_game_dir(paths.find_steam_root())
    target = args.mod
    state = state_mod.load_state()
    r = Report()
    profile_path = Path("profiles") / f"{target}.toml"
    if profile_path.exists():
        try:
            profile = manifest.load_profile(target)
        except OSError as exc:
            raise PathError(f"unknown profile '{target}': {exc}") from exc
        for mod in profile["mods"]:
            mid = mod["id"]
            if mod.get("install", "game") == "manual":
                r.info(f"{mid}: manual install — nothing for erm to remove")
                continue
            try:
                _uninstall_one(game, mid, state, r)
            except PathError as exc:
                r.warn(f"{mid}: {exc}")
    else:
        _uninstall_one(game, target, state, r)
    state_mod.write_state(Path("installed.json"), state)
    print(r.render(as_json=args.json))
    print("\nSafety check (erm doctor):")
    dr = run_doctor(game, Report())
    print(dr.render(as_json=args.json))
    return 0


def cmd_switch(args):
    """Uninstall every mod currently recorded as installed, then apply a new
    profile — the clean way to move between mod stacks without leftovers
    from the old one lingering in Game/."""
    game = paths.find_game_dir(paths.find_steam_root())
    state = state_mod.load_state()
    r = Report()
    for mid in list(state.keys()):
        # One broken installed.json entry (empty file list, no vendor archive
        # to derive from) must not abort the whole switch. Warn, drop it from
        # state anyway — a PathError here means there was nothing on disk to
        # remove, so forgetting it just clears the stale record — and keep
        # uninstalling the rest so we start the new profile from a clean slate.
        try:
            _uninstall_one(game, mid, state, r)
        except PathError as exc:
            r.warn(f"{mid}: {exc}")
            state_mod.forget(state, mid)
    state_mod.write_state(Path("installed.json"), state)
    r.info(f"switching to {args.profile}")
    print(r.render(as_json=args.json))
    return cmd_apply(type("A", (), {"profile": args.profile, "json": args.json})())


def cmd_verify(args):
    lock = manifest.load_lock("mods.lock.toml")
    r = Report()
    for mod_id, meta in lock.items():
        asset = meta.get("asset")
        if not asset:
            r.warn(f"{mod_id}: no asset recorded in lockfile")
            continue
        p = Path("vendor") / asset
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
    src = Path("backups") / args.backup
    if not src.exists():
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


def cmd_harden(args):
    game = paths.find_game_dir(paths.find_steam_root())
    r = Report()
    if harden.is_hardened(game):
        r.info("already hardened — re-asserting the immutable flag")
    else:
        harden.harden_swap(game)
        r.ok("backed up start_protected_game.exe and swapped in an eldenring.exe copy")
    spg = game / "start_protected_game.exe"
    harden.set_immutable(spg, True)   # interactive sudo
    r.ok("start_protected_game.exe is now immutable — Steam Verify/patch can't restore EAC")
    r.warn("run `erm unharden` before any Steam game update, or the update will fail on the immutable file")
    r.warn("vanilla online (invasions/summons) is disabled while hardened")
    print(r.render(as_json=args.json))
    print("\nSafety check (erm doctor):")
    print(run_doctor(game, Report()).render(as_json=args.json))
    return 0


def cmd_unharden(args):
    game = paths.find_game_dir(paths.find_steam_root())
    r = Report()
    spg = game / "start_protected_game.exe"
    if harden.is_hardened(game):
        harden.set_immutable(spg, False)   # interactive sudo, remove immutable FIRST
        harden.unharden_restore(game)
        r.ok("removed immutable flag and restored the real start_protected_game.exe (EAC)")
    else:
        r.info("not hardened — nothing to restore")
    print(r.render(as_json=args.json))
    print("\nSafety check (erm doctor):")
    print(run_doctor(game, Report()).render(as_json=args.json))
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
    f.add_argument("--update", action="store_true",
                    help="ignore the lockfile pin and fetch the latest release")
    f.set_defaults(func=cmd_fetch)
    ap = subparsers.add_parser("apply", help="install the fetched mods into Game/")
    ap.add_argument("profile", nargs="?", default="seamless-only")
    ap.set_defaults(func=cmd_apply)
    up = subparsers.add_parser("update", help="fetch the latest Seamless Co-op, re-pin, and install it")
    up.add_argument("profile", nargs="?", default="seamless-only")
    up.set_defaults(func=cmd_update)
    un = subparsers.add_parser("uninstall", help="remove an installed mod's (or whole profile's) files from Game/")
    un.add_argument("mod", nargs="?", default="seamless-coop")
    un.set_defaults(func=cmd_uninstall)
    sw = subparsers.add_parser("switch", help="uninstall whatever's installed, then apply a different profile")
    sw.add_argument("profile")
    sw.set_defaults(func=cmd_switch)
    subparsers.add_parser("verify", help="re-hash vendor/ against the lockfile").set_defaults(func=cmd_verify)
    b = subparsers.add_parser("backup", help="snapshot the co-op save")
    b.add_argument("--label", default="")
    b.set_defaults(func=cmd_backup)
    rs = subparsers.add_parser("restore", help="restore a save snapshot")
    rs.add_argument("backup")
    rs.set_defaults(func=cmd_restore)
    subparsers.add_parser("quarantine", help="move the vanilla save out of harm's way").set_defaults(func=cmd_quarantine)
    subparsers.add_parser(
        "harden",
        help="swap in a non-EAC launcher and lock it immutable (sudo)",
    ).set_defaults(func=cmd_harden)
    subparsers.add_parser(
        "unharden",
        help="undo `erm harden`: restore the real EAC launcher (sudo)",
    ).set_defaults(func=cmd_unharden)
