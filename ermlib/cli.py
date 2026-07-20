import json
import shutil
import time
import urllib.error
import zipfile
from pathlib import Path

from . import paths, steam, manifest, github, install, saves, nexus, harden, tidy, me3pkg, me3profile, launch
from . import state as state_mod
from .errors import ErmError, NetworkError, PathError
from .report import Report
from .savefile import SaveFile
from .audit import audit_save
from .doctor import run_doctor
# Re-exported so `cli.LAUNCH_OPTION` keeps resolving for tests.
from .launch import LAUNCH_OPTION, LAUNCH_VALIDATOR, RESHADE_ENV

ME3_DIR = Path("tools") / "me3"


def cmd_launch_option(args):
    try:
        me3_packages = state_mod.has_me3_packages(state_mod.load_state())
    except ErmError:
        me3_packages = False
    try:
        reshade = paths.reshade_active(paths.find_game_dir(paths.find_steam_root()))
    except (PathError, OSError):
        reshade = False
    variants = launch.build_variants(launch.find_me3(), reshade, me3_packages)
    if getattr(args, "json", False):
        print(json.dumps(variants, indent=2, sort_keys=True))
        return 0
    print(launch.render(variants))
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
    try:
        state = state_mod.load_state()
    except ErmError as exc:
        r.warn(str(exc))
        state = {}
    if state:
        r.info(f"{len(state)} mod(s) installed:")
        for mid in sorted(state):
            e = state[mid]
            r.info(f"  {mid} {e.get('version', '?')} ({e.get('kind', 'game')})")
        if state_mod.has_me3_packages(state):
            r.info("launch: me3-mode (me3 packages installed) — see `erm launch-option`")
    else:
        r.info("no mods recorded in installed.json")
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


def _profile_needs_fetch(profile, lock, vendor=Path("vendor")):
    """True if any auto-installable mod in `profile` isn't on disk yet — it has
    no lock asset, or its recorded vendor archive is missing. Manual-install
    mods never fetch, so they don't count. Drives apply's auto-fetch."""
    vendor = Path(vendor)
    for mod in profile["mods"]:
        if mod.get("install", "game") == "manual":
            continue
        meta = lock.get(mod["id"])
        asset = meta.get("asset") if meta else None
        if not asset or not (vendor / asset).exists():
            return True
    return False


def fetch_profile(profile_name, vendor, lock_path, profiles_base=Path("profiles"),
                   update=False, nexus_api_key=None, only_missing=False):
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
        if only_missing:
            # Auto-fetch mode (apply/switch): leave a mod that's already pinned
            # AND on disk alone — no network, no re-verify. Explicit `erm fetch`
            # (only_missing=False) still re-checks every pin against upstream.
            locked = lock.get(mod["id"])
            asset_name = locked.get("asset") if locked else None
            if asset_name and (vendor / asset_name).exists():
                continue
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
            file_id = mod.get("file_id")
            skip = False
            try:
                if pinned:
                    # Same reproducibility promise as the GitHub pin: verify
                    # against the LOCKED sha256, not anything Nexus reports —
                    # Nexus's files.json carries no hash to trust anyway.
                    files = nexus.list_files(nid, nexus_api_key)
                    if file_id is not None:
                        # A pinned file_id must still select by id here: a mod
                        # can ship several files under one version (a full + a
                        # Lite build), and find_file_by_version would grab the
                        # wrong one — its hash then fails the locked-sha check.
                        f = nexus.find_file_by_id(files, file_id)
                    else:
                        f = nexus.find_file_by_version(files, locked["version"])
                    url = nexus.download_url(nid, f["file_id"], nexus_api_key)
                    dest = vendor / f["file_name"]
                    github.download_verified(url, dest, locked["sha256"])
                    digest = locked["sha256"]
                else:
                    files = nexus.list_files(nid, nexus_api_key)
                    if file_id is not None:
                        # Profile names the exact variant — no guessing needed.
                        f = nexus.find_file_by_id(files, file_id)
                    else:
                        candidates = nexus.main_files(files)
                        if len(candidates) > 1:
                            # e.g. Minimal HUD #148's 32 numbered MAIN
                            # variants — picking one (even by "highest
                            # version") would silently install the wrong
                            # file. List them and make the user choose via
                            # `file_id` instead of guessing.
                            options = "\n".join(
                                f"    id={c['file_id']}  {c['file_name']}"
                                for c in candidates)
                            print(f"! {mod['id']} has multiple MAIN files on Nexus — "
                                  f"set `file_id` in the profile to one of:\n{options}")
                            skip = True
                        else:
                            # No pin and no upstream hash to check against:
                            # trust on first use — download, then hash what
                            # actually landed on disk and pin THAT. Every
                            # later fetch (yours or a friend's, via the
                            # shared lockfile) verifies against it.
                            f = nexus.pick_main_file(files)
                    if not skip:
                        url = nexus.download_url(nid, f["file_id"], nexus_api_key)
                        dest = vendor / f["file_name"]
                        dest.write_bytes(github._fetch_bytes(url))
                        digest = github.sha256_file(dest)
            except (OSError, urllib.error.URLError, ValueError, KeyError) as exc:
                raise NetworkError(f"failed to fetch {mod['id']} from Nexus: {exc}") from exc
            if skip:
                continue
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
    # Two stacks that edit the same underlying file (regulation.bin, for the
    # item/enemy randomizer vs. Clever's Moveset) can't coexist. A profile
    # declares that via `excludes = ["other-profile"]`; refuse here, before any
    # file is written, if the excluded profile's mods are already installed. A
    # bad/missing exclude reference (typo, deleted profile) must not block an
    # otherwise-legit apply — skip it rather than raising.
    for exc_name in profile.get("excludes", []):
        try:
            excluded_profile = manifest.load_profile(exc_name)
        except (OSError, ErmError):
            continue
        excluded_ids = {m["id"] for m in excluded_profile["mods"]}
        clashing = sorted(excluded_ids & state.keys())
        if clashing:
            raise ErmError(
                f"cannot apply '{args.profile}': it excludes '{exc_name}', which is "
                f"installed ({', '.join(clashing)}). Uninstall that first — only one "
                f"can be active at a time (both edit regulation.bin)."
            )
    password = install.read_secret(Path("secrets.env")) if Path("secrets.env").exists() else ""
    r = Report()
    # Auto-fetch anything the profile needs that isn't on disk yet, so apply/switch
    # works without a separate `erm fetch` first. Only the MISSING mods are pulled
    # (only_missing=True) — present, pinned ones are left untouched, so a fully
    # fetched profile still applies offline. A fetch failure isn't fatal: warn and
    # install whatever's already present.
    if _profile_needs_fetch(profile, lock):
        print(f"fetching missing mods for {args.profile}…")
        try:
            lock = fetch_profile(args.profile, Path("vendor"), Path("mods.lock.toml"),
                                 only_missing=True)
        except ErmError as exc:
            # Any fetch problem (network down, a stale pin failing its hash check,
            # an unknown source) — warn and install what's already present rather
            # than aborting the whole apply. A failed pin leaves nothing on disk
            # (download_verified fails closed), so the loop just skips that mod.
            r.warn(f"auto-fetch incomplete ({exc}) — installing what's already present")
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
            # Record it (kind="randomizer") so the mutual-exclusion guard sees a
            # randomizer as installed and `erm uninstall` can remove its tools/
            # dir — even though the generator itself never lands in Game/.
            state_mod.record_randomizer(state, mid, meta.get("version"),
                                        asset, str(Path("tools") / mid))
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
            r.ok(f"{mid}: extracted to tools/{mid}/ (loader — replaces the Steam launch-option method)")
            r.info("me3 profile is generated as tools/me3/erm-coop.me3 by erm — launch via me3 "
                    "(see me3.help for the Linux setup)")
            continue
        if kind == "me3-native":
            try:
                native = me3pkg.install_me3_native(vpath, mid, ME3_DIR, dll=mod.get("dll"))
            except PathError as exc:
                r.warn(str(exc))
                continue
            except (OSError, zipfile.BadZipFile) as exc:
                r.warn(f"{mid}: install failed ({exc})")
                continue
            state_mod.record_me3_native(state, mid, meta.get("version", "?"), asset, native)
            r.ok(f"{mid} {meta.get('version', '')} → me3 native (chainloaded)")
            continue
        if kind == "me3-package":
            try:
                package, has_reg = me3pkg.install_me3_package(vpath, mid, ME3_DIR, subdir=mod.get("subdir"))
            except PathError as exc:
                r.warn(str(exc))
                continue
            except (OSError, zipfile.BadZipFile) as exc:
                r.warn(f"{mid}: install failed ({exc})")
                continue
            state_mod.record_me3_package(state, mid, meta.get("version", "?"), asset, package)
            if has_reg:
                r.warn(f"{mid}: contains regulation.bin — that's a SHARED mod (every co-op "
                       f"player needs the identical file), not a client-side cosmetic")
            r.ok(f"{mid} {meta.get('version', '')} → me3 package")
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
    try:
        me3profile.reconcile(state, ME3_DIR, game)
    except OSError as exc:
        r.warn(f"could not regenerate the me3 profile ({exc}) — run `erm apply` again")
    if installed_seamless and not password:
        r.warn("no COOP_PASSWORD in secrets.env — password left blank")
    # A loader mod (Elden Mod Loader's dinput8.dll, or me3) can be picked up by
    # an accidental vanilla "Play" click, which lets EAC see the injected DLL.
    # Harden automatically so that stray launch can't fire EAC. seamless-only
    # (no loader) never trips this. Don't re-harden if already hardened — the
    # backup-once invariant in harden_swap already protects the real EAC
    # backup, but skipping here avoids pointlessly re-prompting for sudo.
    needs_harden = any(m.get("kind") == "loader" for m in profile["mods"])
    if needs_harden and not getattr(args, "no_harden", False) and not harden.is_hardened(game):
        r.info("this profile loads mods via a proxy DLL — hardening so an accidental vanilla "
               "launch can't fire EAC on the injected DLLs (run `erm unharden` before a Steam "
               "game update; vanilla online is disabled while hardened)")
        try:
            harden.harden_swap(game)                                          # fs-only, no privilege
            harden.set_immutable(game / "start_protected_game.exe", True)      # interactive sudo
            r.ok("hardened: start_protected_game.exe swapped to an eldenring copy and locked immutable")
        except ErmError as exc:
            # harden_swap may have succeeded even if the immutable step failed
            # — the swap alone still blocks an accidental EAC launch; just tell
            # the user the lock didn't complete. Mods are already installed, so
            # this warns rather than aborting the rest of apply.
            r.warn(f"auto-harden incomplete: {exc} — run `erm harden` to finish (or `erm unharden` to revert)")
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
        # _install_ersc writes installed.json itself (load/record/write is
        # self-contained), so reload right after to reconcile against the
        # state it JUST wrote — not whatever this function's stale `before`
        # snapshot would imply. Without this, a me3-mode profile's
        # erm-coop.me3 keeps its pre-update natives-less form and me3 never
        # chainloads ersc.dll even though installed.json now says it's there.
        state = state_mod.load_state()
        try:
            me3profile.reconcile(state, ME3_DIR, game)
        except OSError as exc:
            r.warn(f"could not regenerate the me3 profile ({exc}) — run `erm apply` again")
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
    if entry and entry.get("kind") == "me3-native":
        native = entry.get("native")
        if not native:
            r.warn(f"{mod_id}: native entry has no recorded dll path — forgetting it")
            state_mod.forget(state, mod_id)
            return
        # Remove the mod's whole dir, not the dll's parent — the dll often sits
        # in a subfolder, and its sibling ini/lang files have to go too. install
        # always creates natives/<id>/, so that's the unit; the recorded dll only
        # has to prove it lives inside it (installed.json is hand-editable).
        ndir = ME3_DIR / "natives" / mod_id
        try:
            Path(native).resolve().relative_to(ndir.resolve())
            contained = True
        except (ValueError, OSError):
            contained = False
        if not contained:
            r.warn(f"{mod_id}: recorded native {native} is outside {ndir} — refusing to remove")
            state_mod.forget(state, mod_id)
            return
        if ndir.is_dir():
            try:
                shutil.rmtree(ndir)
                r.ok(f"{mod_id}: removed me3 native")
            except OSError as exc:
                r.warn(f"{mod_id}: could not remove {ndir} ({exc}) — left on disk")
        else:
            r.ok(f"{mod_id}: me3 native already gone")
        state_mod.forget(state, mod_id)
        return
    if entry and entry.get("kind") == "me3-package":
        pkg = Path(entry["package"])
        # installed.json can be hand-edited (or corrupted), so re-validate
        # before rmtree — same reasoning as the files-list containment check
        # below, just against the me3 packages dir instead of Game/.
        mods_root = (ME3_DIR / "mods").resolve()
        try:
            pkg.resolve().relative_to(mods_root)
            contained = True
        except (ValueError, OSError):
            contained = False
        if not contained:
            r.warn(f"{mod_id}: recorded package {pkg} is outside {mods_root} — refusing to remove")
            state_mod.forget(state, mod_id)
            return
        if pkg.is_dir():
            try:
                shutil.rmtree(pkg)
                r.ok(f"{mod_id}: removed me3 package")
            except OSError as exc:
                r.warn(f"{mod_id}: could not remove {pkg} ({exc}) — left on disk")
        else:
            r.ok(f"{mod_id}: me3 package already gone")
        state_mod.forget(state, mod_id)
        return
    if entry and entry.get("kind") == "randomizer":
        tdir_str = entry.get("tools")
        if not tdir_str:
            r.warn(f"{mod_id}: randomizer entry has no recorded tools path — forgetting it")
            state_mod.forget(state, mod_id)
            return
        tdir = Path(tdir_str)
        # Same containment re-validation as the me3-package branch: installed.json
        # can be hand-edited, so confirm the dir is under tools/ before rmtree.
        tools_root = Path("tools").resolve()
        try:
            tdir.resolve().relative_to(tools_root)
            contained = True
        except (ValueError, OSError):
            contained = False
        if not contained:
            r.warn(f"{mod_id}: recorded generator {tdir} is outside {tools_root} — refusing to remove")
            state_mod.forget(state, mod_id)
            return
        if tdir.is_dir():
            try:
                shutil.rmtree(tdir)
                r.ok(f"{mod_id}: removed randomizer generator")
            except OSError as exc:
                r.warn(f"{mod_id}: could not remove {tdir} ({exc}) — left on disk")
        else:
            r.ok(f"{mod_id}: randomizer generator already gone")
        state_mod.forget(state, mod_id)
        return
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
            if mid not in state:
                # Only mods recorded in installed.json were extracted into
                # Game/. me3/randomizer install to tools/ (never recorded),
                # manual mods are never extracted at all, and a not-yet-applied
                # mod has nothing on disk — none appear in state, so there's
                # nothing here to remove. Don't fall into _uninstall_one's
                # vendor-archive fallback: for a tools mod it would open the
                # wrong zip and report a bogus "removed 0 file(s)". The
                # single-mod `erm uninstall <mod>` path keeps that recovery for
                # a lost manifest; profile uninstall deliberately doesn't guess.
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
    try:
        me3profile.reconcile(state, ME3_DIR, game)
    except OSError as exc:
        r.warn(f"could not regenerate the me3 profile ({exc}) — run `erm apply` again")
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
    try:
        me3profile.reconcile(state, ME3_DIR, game)
    except OSError as exc:
        r.warn(f"could not regenerate the me3 profile ({exc}) — run `erm apply` again")
    r.info(f"switching to {args.profile}")
    print(r.render(as_json=args.json))
    return cmd_apply(type("A", (), {
        "profile": args.profile,
        "json": args.json,
        "no_harden": getattr(args, "no_harden", False),
    })())


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


def cmd_tidy(args):
    """Remove runtime cruft `erm uninstall`/`switch` leave behind that
    installed.json never tracked — per-mod log dirs, ERSC crash dumps, loader
    logs. Dry-run by default (lists candidates); `--apply` actually deletes.
    Every candidate already passed find_cruft's containment/recorded/critical
    checks, so this loop just acts on what it's handed."""
    game = paths.find_game_dir(paths.find_steam_root())
    state = state_mod.load_state()
    recorded = {rel for meta in state.values() for rel in meta.get("files", [])}
    cruft = tidy.find_cruft(game, recorded)
    r = Report()
    if not cruft:
        r.ok("nothing to tidy — no orphaned mod logs/dirs found")
        print(r.render(as_json=args.json))
        return 0
    verb = "removing" if args.apply else "would remove"
    for c in cruft:
        r.info(f"{verb}: {c.relative_to(game)}")
    if not args.apply:
        print(r.render(as_json=args.json))
        print(f"\n{len(cruft)} item(s) would be removed (all inside Game/, none recorded in "
              f"installed.json). Re-run `erm tidy --apply` to delete them.")
        return 0
    removed = 0
    for c in cruft:
        try:
            if c.is_dir():
                shutil.rmtree(c)
            else:
                c.unlink()
            removed += 1
        except OSError as exc:
            r.warn(f"could not remove {c.relative_to(game)}: {exc}")
    r.ok(f"tidied {removed} item(s)")
    print(r.render(as_json=args.json))
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
    subparsers.add_parser(
        "launch-option", help="print every Steam launch option"
    ).set_defaults(func=cmd_launch_option)
    f = subparsers.add_parser("fetch", help="download + verify a profile's mods")
    f.add_argument("profile", nargs="?", default="seamless-only")
    f.add_argument("--update", action="store_true",
                    help="ignore the lockfile pin and fetch the latest release")
    f.set_defaults(func=cmd_fetch)
    ap = subparsers.add_parser("apply", help="install the fetched mods into Game/")
    ap.add_argument("profile", nargs="?", default="seamless-only")
    ap.add_argument("--no-harden", action="store_true",
                     help="skip auto-harden even if the profile loads mods via a proxy DLL/me3")
    ap.set_defaults(func=cmd_apply)
    up = subparsers.add_parser("update", help="fetch the latest Seamless Co-op, re-pin, and install it")
    up.add_argument("profile", nargs="?", default="seamless-only")
    up.set_defaults(func=cmd_update)
    un = subparsers.add_parser("uninstall", help="remove an installed mod's (or whole profile's) files from Game/")
    un.add_argument("mod", nargs="?", default="seamless-coop")
    un.set_defaults(func=cmd_uninstall)
    sw = subparsers.add_parser("switch", help="uninstall whatever's installed, then apply a different profile")
    sw.add_argument("profile")
    sw.add_argument("--no-harden", action="store_true",
                     help="skip auto-harden even if the new profile loads mods via a proxy DLL/me3")
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
    td = subparsers.add_parser(
        "tidy",
        help="remove orphaned mod logs/runtime dirs left behind by uninstall (dry-run by default)",
    )
    td.add_argument("--apply", action="store_true",
                     help="actually delete the candidates (default: list only)")
    td.set_defaults(func=cmd_tidy)
