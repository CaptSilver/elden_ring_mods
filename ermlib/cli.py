import argparse
import http.client
import json
import shutil
import time
import urllib.error
import zipfile
from pathlib import Path

from . import paths, steam, manifest, github, install, saves, nexus, harden, tidy, me3pkg, me3profile, launch, conflicts, merge
from . import state as state_mod
from .errors import ErmError, NetworkError, PathError
from .report import Report
from .savefile import SaveFile
from .audit import audit_save
from .doctor import run_doctor
from . import doctor as doctor_mod
from . import gamebuild
from .gamebuild import GameBuildError
from . import heal
# Re-exported so `cli.LAUNCH_OPTION` keeps resolving for tests.
from .launch import LAUNCH_OPTION, LAUNCH_VALIDATOR, RESHADE_ENV

ME3_DIR = Path("tools") / "me3"
# Relative to the cwd, like everything else erm writes: `erm backups` has to
# read the directory `erm backup` writes.
BACKUPS_DIR = Path("backups")

# What a download can fail with. http.client.HTTPException is the one that keeps
# getting forgotten: IncompleteRead (a server closing cleanly mid-body) inherits
# from bare Exception, not OSError, so it walked past a guard listing the obvious
# network errors and out through apply's "install what's already present"
# fallback as a raw traceback. One tuple, so the two guards can't drift.
_FETCH_FAILURES = (OSError, urllib.error.URLError, http.client.HTTPException,
                   ValueError, KeyError)


def _emit(args, r, doctor=None):
    """Print one command's result and return its exit code. Every handler's
    single exit point, so none of them can grow a second output shape.

    --json has to be ONE document: a command that also runs the doctor nests
    that report under "doctor" rather than printing a prose header and a
    second document after the first, which is what made `erm --json apply`
    unreadable to json.loads. Human output is unchanged — the two reports with
    the header between them.

    The code reported is the doctor's when there is one: it speaks for the
    install's safety, which outranks whether the command itself did its job.
    """
    if getattr(args, "json", False):
        payload = r.to_dict()
        if doctor is not None:
            payload["doctor"] = doctor.to_dict()
        print(json.dumps(payload))
    else:
        if not r.stream:
            print(r.render())
        if doctor is not None:
            print("\nSafety check (erm doctor):")
            print(doctor.render())
    return doctor.exit_code if doctor is not None else r.exit_code


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
        # Deliberately NOT the {"worst", "items"} report shape every other
        # command emits: this one answers a question rather than reporting on a
        # run, and a consumer wants the launch strings keyed by variant, not a
        # list of prose lines to grep. The only command whose --json is data.
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
    # An item, not a trailing print: the caveat is the most important thing the
    # audit says, so a --json consumer has to receive it too.
    r.info(res.caveat)
    return _emit(args, r)


def cmd_status(args):
    root = paths.find_steam_root()
    m = steam.read_appmanifest(root)
    r = Report()
    r.info(f"game installed: {m.get('installed')}  buildid={m.get('buildid')}")
    try:
        live = gamebuild.identify(paths.find_game_dir(root), root)
        r.info(f"game build: {live.app} (regulation {live.regulation}, exe {live.exe})")
    except ErmError as exc:          # GameBuildError is one of these
        r.warn(f"can't identify the game build: {exc}")
    for cs in steam.cloud_saves(root):
        r.info(f"cloud save: account {cs['account_id']} {cs['relpath']} ({cs['size']} B)")
    try:
        state = state_mod.load_state()
    except ErmError as exc:
        r.warn(str(exc))
        state = {}
    if state:
        mods = sorted(state_mod.mod_ids(state))
        try:
            stamped = state_mod.stamped_build(state)
        except ErmError as exc:
            # A corrupt stamp must not take the whole status listing down with
            # it -- the mod list is exactly what you want to see when
            # installed.json is in a bad way.
            r.warn(str(exc))
            stamped = None
        if stamped:
            r.info(f"stack built for: {stamped.app}")
        r.info(f"{len(mods)} mod(s) installed:")
        for mid in mods:
            e = state[mid]
            r.info(f"  {mid} {e.get('version', '?')} ({e.get('kind', 'game')})")
        if state_mod.has_me3_packages(state):
            r.info("launch: me3-mode (me3 packages installed) — see `erm launch-option`")
    else:
        r.info("no mods recorded in installed.json")
    return _emit(args, r)


def cmd_doctor(args):
    root = paths.find_steam_root()
    game = paths.find_game_dir(root)
    r = run_doctor(game, Report())
    try:
        live = gamebuild.identify(game, root)
    except GameBuildError as exc:
        r.warn(f"can't identify the game build: {exc}")
    else:
        try:
            state = state_mod.load_state()
        except ErmError as exc:
            r.warn(str(exc))
            state = {}
        try:
            stamped = state_mod.stamped_build(state)
        except ErmError as exc:
            r.warn(str(exc))
            stamped = None
        doctor_mod.run_build_checks(game, stamped, live, r, state=state,
                                    lock=manifest.load_lock("mods.lock.toml"))
    return _emit(args, r)


def cmd_refresh(args):
    root = paths.find_steam_root()
    game = paths.find_game_dir(root)
    r = Report()
    live = gamebuild.identify(game, root)
    stamped = state_mod.stamped_build(state_mod.load_state())
    stale = doctor_mod.launcher_is_stale(game)
    actions = heal.plan_heal(stamped, live,
                             reharden=not args.no_reharden,
                             launcher_stale=bool(stale))
    if stale and args.no_reharden:
        # The flag decides what goes in the PLAN, not what refresh is allowed
        # to notice. Letting it swallow the finding as well is how a stale swap
        # got a green all-clear from the one command whose job is naming what
        # needs bringing forward. Steam runs that binary.
        swapped, real = stale
        r.warn(f"hardened launcher is stale: start_protected_game.exe {swapped}, "
               f"eldenring.exe {real} — run `erm unharden && erm harden` to put it "
               f"back on the game's build")
    if not actions:
        if stamped is None:
            # An unstamped stack plans nothing because there is no recorded
            # build to have drifted from -- not because it is up to date.
            r.info(f"stack build not recorded — can't tell whether this stack "
                   f"matches build {live.app}. Run `erm apply` to stamp it.")
        elif stale:
            # "nothing to do" would argue with the warning directly above it.
            r.info(f"the stack itself is built for {live.app}")
        else:
            r.ok(f"stack is already built for {live.app} — nothing to do")
        return _emit(args, r)
    if stamped is None and not all(a.kind == "refuse" for a in actions):
        # Still worth saying even though something IS planned: the plan below
        # may be about the launcher alone, which tells the reader nothing about
        # whether the stack itself was built for this game.
        r.info(f"stack build not recorded — can't tell whether this stack "
               f"matches build {live.app}. Run `erm apply` to stamp it.")
    for a in actions:
        if a.kind == "refuse":
            r.fail(a.detail)
        else:
            r.info(f"{a.kind}: {a.detail}")
    if all(a.kind == "refuse" for a in actions):
        # Nothing to execute: the refusal is the whole outcome. Following it
        # with "executing a heal is not wired up yet" would point at a missing
        # feature nobody was asking for here.
        return _emit(args, r)
    if args.dry_run:
        r.info("dry run — nothing was changed")
        return _emit(args, r)
    # `refresh` only reports. `apply` runs every step above except the repin:
    # it adopts the baseline, gates the layouts, rebuilds each merge onto the
    # installed build and verifies the result, all as part of a normal install.
    if all(a.kind == "reharden" for a in actions):
        # apply cannot carry this one out: it only hardens an install that is
        # not hardened yet, and the swap is chattr +i so it could not overwrite
        # the stale copy anyway. Naming apply here would send the reader in a
        # circle.
        r.info("run `erm unharden && erm harden` to put the hardened launcher back "
               "on the game's build — `erm apply` can't: it only hardens an install "
               "that isn't hardened yet, and the swap is immutable")
    else:
        r.info("run `erm apply` to rebase every merge onto the installed build — it "
               "gates the param layouts first and verifies the rebase after. It does "
               "not re-pin: run `erm update` first if a mod needs a newer version to "
               "fit this build")
    return _emit(args, r)


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


def _already_vendored(dest, digest):
    """`dest` already holds exactly the bytes `digest` names.

    The same check `erm verify` runs, done before spending a download on it. It
    has to be the hash and not just the name: an interrupted write leaves a
    short archive at the right path, and adopting that is the one failure a
    re-download exists to prevent. An empty digest falls through so the
    fail-closed download still happens.
    """
    return bool(digest) and dest.exists() and github.sha256_file(dest) == digest


def fetch_profile(profile_name, vendor, lock_path, profiles_base=Path("profiles"),
                   update=False, nexus_api_key=None, only_missing=False, outcomes=None,
                   report=None):
    """`outcomes`, if given, is filled in with mod_id -> "checked" | "skipped":
    whether upstream was actually contacted for that mod. A caller reporting on
    this run can only speak for the mods it visited, and only a "checked" one
    has a version worth calling current -- the rest keep their old pin because
    erm declined to look, not because it looked and found nothing newer."""
    def _seen(mod_id, outcome):
        if outcomes is not None:
            outcomes[mod_id] = outcome
    # Progress is report items, not bare prints, so a caller in --json mode gets
    # them inside its document instead of as prose wrapped around it. Without a
    # caller's report we stream, which is what a download of this size needs:
    # the same lines at the same moment as before.
    out = report if report is not None else Report(stream=True)
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
                _seen(mod["id"], "skipped")
                continue
        if mod["source"] == "github":
            locked = lock.get(mod["id"])
            pinned = not update and locked and locked.get("version")
            # me3 publishes the Linux build as .tar.gz beside the Windows .zip,
            # so the extension is per-mod. Defaulting it to .zip would fetch the
            # wrong platform's build and report success.
            suffix = mod.get("asset_suffix", ".zip")
            try:
                if pinned:
                    # Reproducibility promise: everyone who clones the repo and
                    # runs `erm fetch` gets THIS exact release, not whatever's
                    # newest. Verify against the sha we already trust — if
                    # upstream mutated the tagged asset, fail closed.
                    rel = github.release_by_tag(mod["repo_id"], locked["version"])
                    asset = github.pick_asset(rel, suffix=suffix,
                                               name_hint=mod.get("asset_match"))
                    digest = locked.get("sha256") or ""
                else:
                    rel = github.latest_release(mod["repo_id"])
                    asset = github.pick_asset(rel, suffix=suffix,
                                               name_hint=mod.get("asset_match"))
                    digest = (asset.get("digest") or "").removeprefix("sha256:")
                dest = vendor / f'{mod["id"]}-{rel["tag"]}{suffix}'
                cached = _already_vendored(dest, digest)
                if not cached:
                    github.download_verified(asset["url"], dest, digest)
            except _FETCH_FAILURES as exc:
                raise NetworkError(f"failed to fetch {mod['id']} from GitHub: {exc}") from exc
            manifest.set_mod(lock, mod["id"], version=rel["tag"],
                             asset=dest.name, sha256=digest, source="github")
            note = " (pinned)" if pinned else ""
            _seen(mod["id"], "checked")
            out.ok(f"{mod['id']} {rel['tag']}{note} "
                   f"{'cached, verified' if cached else 'verified'} → {dest.name}")
        elif mod["source"] == "nexus":
            nid = mod.get("nexus_id")
            if not nexus_api_key:
                # Free accounts can't hit the download_link.json endpoint —
                # same manual-download instruction as always.
                out.warn(f"{mod['id']} is a manual Nexus download: "
                         f"https://www.nexusmods.com/eldenring/mods/{nid} "
                         f"— download the archive into {vendor}/ , then re-run apply.")
                _seen(mod["id"], "skipped")
                continue
            locked = lock.get(mod["id"])
            pinned = not update and locked and locked.get("version")
            # Once a repin has moved the pin the lockfile is where it lives;
            # the profile's file_id is only where it started. Preferring the
            # profile here would re-select the superseded file and fail it
            # against the hash the repin locked.
            file_id = (locked or {}).get("file_id") or mod.get("file_id")
            skip = False
            cached = False
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
                    digest = locked["sha256"]
                    # The link is resolved either way: it is a small JSON call,
                    # and it is what proves this run selected the pinned file
                    # rather than skipping the question. Only the archive is
                    # skipped, which is the part measured in gigabytes.
                    cached = _already_vendored(dest, digest)
                    if not cached:
                        github.download_verified(url, dest, digest)
                else:
                    files = nexus.list_files(nid, nexus_api_key)
                    current = None
                    if file_id is not None:
                        current = nexus.find_file_by_id(files, file_id)
                    choice = nexus.resolve_pin(current, files, nid,
                                               frozen=bool(mod.get("freeze")))
                    if choice.action == "ambiguous":
                        # Ambiguity keeps the current pin: the worst outcome
                        # must be a missed update visible in the report, never
                        # a substituted mod.
                        options = "\n".join(
                            f"    id={c['file_id']}  {c['file_name']}"
                            for c in choice.candidates)
                        out.warn(f"{mod['id']}: {choice.reason} — "
                                 f"set `file_id` in the profile to one of:\n{options}"
                                 if choice.candidates else
                                 f"{mod['id']}: {choice.reason} — keeping the current pin")
                        skip = True
                    else:
                        f = choice.file
                        if choice.action == "repin":
                            out.info(f"{mod['id']} repin {file_id} -> "
                                     f"{f['file_id']} ({f.get('version')}) [{choice.reason}]")
                            if mod.get("requires_all_players"):
                                out.warn(f"{mod['id']} is required of all players — "
                                         "your partner must pull the lockfile and re-apply")
                    if not skip:
                        url = nexus.download_url(nid, f["file_id"], nexus_api_key)
                        dest = vendor / f["file_name"]
                        locked_sha = (locked or {}).get("sha256") or ""
                        # "unchanged" plus the locked bytes already on disk is
                        # the whole job done. Keeping the LOCKED digest instead
                        # of re-hashing the wire is also what stops an upstream
                        # swap under a stable file_id being adopted silently --
                        # including for a pin the profile declared frozen.
                        cached = (choice.action == "unchanged"
                                  and _already_vendored(dest, locked_sha))
                        if cached:
                            digest = locked_sha
                        else:
                            # No pin and no upstream hash to check against: trust
                            # on first use — hash what actually lands on disk
                            # and pin THAT. Every later fetch (yours or a
                            # friend's, via the shared lockfile) verifies
                            # against it. The digest comes back from the
                            # download itself, so the archive is hashed as it
                            # streams past rather than read off disk again.
                            digest = github.download_stream(url, dest)
            except _FETCH_FAILURES as exc:
                raise NetworkError(f"failed to fetch {mod['id']} from Nexus: {exc}") from exc
            if skip:
                _seen(mod["id"], "skipped")
                continue
            manifest.set_mod(lock, mod["id"], version=f["version"],
                             asset=f["file_name"], sha256=digest, source="nexus",
                             file_id=f.get("file_id"))
            verb = "cached, verified" if cached else ("(pinned) verified" if pinned else "fetched")
            _seen(mod["id"], "checked")
            out.ok(f"{mod['id']} v{f['version']} {verb} → {f['file_name']}")
        else:
            raise PathError(f"unknown source '{mod['source']}' for mod '{mod['id']}'")
    manifest.write_lock(lock_path, lock)
    return lock


def cmd_fetch(args):
    # Streams in human mode so a multi-gigabyte pull shows progress; --json
    # holds the items back so the whole run comes out as one document.
    r = Report(stream=not getattr(args, "json", False))
    fetch_profile(args.profile, Path("vendor"), Path("mods.lock.toml"),
                  update=args.update, report=r)
    return _emit(args, r)


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


def _persist(state, game, r):
    """Write installed.json and regenerate the me3 profile from it.

    The me3 profile is a pure function of state, so the two have to move
    together -- including down an abort path, where state has just forgotten
    the merged package whose directory was wiped. Regenerating is best-effort:
    the error being aborted with is what the caller needs to see, so a failed
    profile write warns rather than replacing it.
    """
    state_mod.write_state(Path("installed.json"), state)
    try:
        me3profile.reconcile(state, ME3_DIR, game)
    except OSError as exc:
        r.warn(f"could not regenerate the me3 profile ({exc}) — erm-coop.me3 may still "
               f"name a package that is no longer on disk; re-run `erm apply`")


def cmd_apply(args, r=None):
    """Install a profile, printing what was found even if the run aborts.

    Everything apply notices accumulates in one Report. That Report is a local,
    so an abort used to take every warning with it and surface the exception
    alone -- including on the refusal whose own text points at "a failed install
    reported above", which the reader then could not find.

    `r` lets switch hand its own uninstall report in, so the whole switch comes
    out as one report (and, under --json, one document) rather than two.
    """
    if r is None:
        # Live output in human mode: apply installs mod after mod and can spend
        # minutes auto-fetching, and holding it all back to the end reads as a
        # hang. --json buffers so the run is a single document.
        r = Report(stream=not getattr(args, "json", False))
    try:
        return _apply(args, r)
    except ErmError:
        if not r.stream:
            rendered = r.render(as_json=getattr(args, "json", False))
            if rendered.strip():
                print(rendered)
        raise


# Every `install` mode the loop in _apply handles. The chain there ends in the
# generic extract-into-Game/ branch, so without this an unrecognised mode -- a
# profile typo, or one dropped from the code while a profile still names it --
# unpacked its archive over the game directory and reported success. Writing
# files nobody asked for is the least safe thing that loop can do, so an unknown
# mode has to stop the run instead of falling through.
INSTALL_MODES = frozenset({
    "game", "mods", "manual", "randomizer", "me3-host", "me3-native", "me3-package",
})


def _apply(args, r):
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
        clashing = sorted(excluded_ids & set(state_mod.mod_ids(state)))
        if clashing:
            raise ErmError(
                f"cannot apply '{args.profile}': it excludes '{exc_name}', which is "
                f"installed ({', '.join(clashing)}). Uninstall that first — only one "
                f"can be active at a time (both edit regulation.bin)."
            )
    password = install.read_secret(Path("secrets.env")) if Path("secrets.env").exists() else ""
    # Auto-fetch anything the profile needs that isn't on disk yet, so apply/switch
    # works without a separate `erm fetch` first. Only the MISSING mods are pulled
    # (only_missing=True) — present, pinned ones are left untouched, so a fully
    # fetched profile still applies offline. A fetch failure isn't fatal: warn and
    # install whatever's already present.
    if _profile_needs_fetch(profile, lock):
        r.info(f"fetching missing mods for {args.profile}…")
        try:
            lock = fetch_profile(args.profile, Path("vendor"), Path("mods.lock.toml"),
                                 only_missing=True, report=r)
        except ErmError as exc:
            # Any fetch problem (network down, a stale pin failing its hash check,
            # an unknown source) — warn and install what's already present rather
            # than aborting the whole apply. A failed pin leaves nothing on disk
            # (download_verified fails closed), so the loop just skips that mod.
            r.warn(f"auto-fetch incomplete ({exc}) — installing what's already present")
    installed_seamless = False
    # Which me3-package ids actually got a fresh rmtree+re-extract THIS run --
    # as opposed to one still sitting in state only because an earlier apply
    # put it there. A merge's contributor needs to be in this set before we
    # trust its on-disk copy as a faithful source; see the merge-resolution
    # block below for why.
    reinstalled_packages = set()
    for mod in profile["mods"]:
        mid = mod["id"]
        kind = mod.get("install", "game")
        if kind not in INSTALL_MODES:
            raise ErmError(f"{mid}: unknown install mode {kind!r} "
                           f"(known: {', '.join(sorted(INSTALL_MODES))})")
        if kind == "manual":
            r.info(f"{mid}: manual — install it per the mod's own README")
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
        # Only once the archive is confirmed present: this deletes the previous
        # install, so doing it before the checks above would strip a working mod
        # on a run that then had nothing to put back.
        if not _drop_stale_install(game, mid, kind, state, r):
            continue
        # The randomizer generator is a tool, not a Game/ mod: extract it to
        # tools/<mid>/ instead of Game/.
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
        if kind == "me3-host":
            # Steam starts the native binary and me3 builds the Proton command
            # itself. Installed rather than merely unpacked, because the path
            # Steam invokes is fixed and outside this repo. The Windows pieces
            # me3 chainloads go with it, into the data dir me3 reads them from.
            try:
                binary = me3pkg.install_me3_host(vpath, launch.ME3_BINDIR,
                                                 launch.ME3_DATADIR)
            except PathError as exc:
                r.warn(str(exc))
                continue
            except (OSError, zipfile.BadZipFile) as exc:
                r.warn(f"{mid}: install failed ({exc})")
                continue
            r.ok(f"{mid} {meta.get('version', '')} → {binary} (native launcher)")
            r.info("me3 profile is generated as tools/me3/erm-coop.me3 by erm — launch via me3 "
                    "(`erm launch-option` prints the line)")
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
            reinstalled_packages.add(mid)
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
                # Only the mods/ path strips a wrapper dir: Elden Mod Loader
                # scans mods/*.dll and never recurses, so a mod zipped inside a
                # version-named folder would load nothing. install="game"
                # archives usually ARE a single top-level mods/ dir — stripping
                # there would drop their DLLs into Game/ and unload them.
                files = install.extract_archive(vpath, game, subdir,
                                                strip_wrapper=(kind == "mods"))
        except (OSError, zipfile.BadZipFile) as exc:
            r.warn(f"{mid}: install failed ({exc})")
            continue
        if mid == "seamless-coop":
            installed_seamless = True
        state_mod.record_install(state, mid, meta.get("version", "?"), asset, files,
                                 install=kind)
        r.ok(f"{mid} {meta.get('version', '')} → {subdir or 'Game/'}")
    # Resolve VFS collisions before recording state: every me3 package is on
    # disk by now, and me3 would otherwise mount one file per path and drop the
    # rest silently. Scope this to what me3 will ACTUALLY mount -- everything
    # in state, the same source me3profile.reconcile itself reads -- not just
    # this profile's own mods. A profile applied earlier (and never switched
    # away from or uninstalled) leaves its packages in state and in
    # erm-coop.me3 regardless of what's being applied now, so a collision
    # between one of those and this profile's own mods is exactly as real as
    # one within a single profile; scoping to the current profile's ids alone
    # would silently miss it.
    # Merged output belongs to the mods that produced it, not to whichever
    # profile happens to be applied next. An overlay profile (cosmetic-extras on
    # top of a coop base) declares no merges and shares no mods with the base's
    # contributors, so it has no say over their merged files -- and it must not
    # wipe them, because a successful merge leaves the ONLY remaining copy: the
    # path has already been stripped from every contributor. Carrying them
    # across is what keeps `apply base` then `apply overlay` from silently
    # ending up with no regulation.bin at all.
    #
    # Anything this profile does own still gets cleared, so withdrawing a merge
    # declaration and re-applying still drops the stale output.
    profile_mod_ids = {m["id"] for m in profile["mods"]}
    installed_packages = {mid for mid, _pkg in state_mod.me3_packages(state)
                          if mid != conflicts.MERGED_ID}
    prior_merged = (state.get(conflicts.MERGED_ID) or {}).get("paths") or {}
    carried = {rel: mods for rel, mods in prior_merged.items()
               # Not this profile's business...
               if not (set(mods) & profile_mod_ids)
               # ...and still backed by installed mods, so merged output can't
               # outlive the sources it was built from.
               and set(mods) <= installed_packages}
    # This block opens by wiping the merged package, so from clear_merged() to
    # the end of it every exit -- not just an unresolvable collision -- has to
    # leave state and the me3 profile agreeing with what is actually on disk.
    # See the handler.
    try:
        conflicts.clear_merged(ME3_DIR, keep=carried)
        if carried:
            # Whatever survived the clear is still on disk, so state has to keep
            # saying so -- including down the abort path below, which writes state
            # and re-raises without ever reaching the success-path record.
            state_mod.record_merged(state, f"tools/me3/mods/{conflicts.MERGED_ID}", carried)
        # Forget any merged package from a PRIOR apply right away, in lockstep with
        # the physical dir clear_merged() just wiped — not after the resolve() call
        # below. resolve() can refuse a totally unrelated collision, and the except
        # clause below writes state and re-raises; if forgetting waited until after
        # that try/except, this path would never reach it, and
        # installed.json would keep claiming _merged is installed even though its
        # directory is already gone. Re-recorded below only if this run's merge
        # actually succeeds. Excluded from package_ids below for the same reason:
        # it's this function's own prior output, not a real collision provider.
        if not carried:
            state_mod.forget(state, conflicts.MERGED_ID)
        package_ids = [mid for mid, _pkg in state_mod.me3_packages(state)
                       if mid != conflicts.MERGED_ID]
        # Before the prunes, and well before resolve(): a rename decides which path
        # a file even occupies, so every collision and merge downstream has to see
        # the moved file rather than the one the author happened to ship.
        for renamed in conflicts.apply_renames(ME3_DIR, profile.get("renames", [])):
            r.info(f"renamed {renamed}")
        for pruned in conflicts.apply_prunes(ME3_DIR, profile.get("prunes", [])):
            # No reason attached: a prune drops a path because the profile says so,
            # and the profile's comment carries why. The old wording asserted the
            # file "ships no content of its own", which is true of the vanilla
            # copies it was written for and false of a prune that deliberately
            # gives up real content to settle a collision.
            r.info(f"pruned {pruned}")
        # A game newer than the ancestor the mods branched from means the merge
        # would be built out of game data the install no longer has. Fold every
        # contributor onto the installed game's own regulation instead, so the rows
        # the patch added survive alongside the mods' edits.
        bases = {}
        try:
            live = gamebuild.identify(game, steam_root)
        except GameBuildError as exc:
            live = None
            r.warn(f"could not check the game build ({exc}) — merging against the "
                   "profile's declared ancestor")
        reg_contributors = [(m, (ME3_DIR / "mods" / m / heal.REGULATION).read_bytes())
                            for m in package_ids
                            if (ME3_DIR / "mods" / m / heal.REGULATION).is_file()]
        # Ask for the ancestor only when a merge is going to fold onto it: one
        # contributor is no collision, resolve() leaves it alone, and reading
        # an archive nothing is about to consume would refuse applies that have
        # no merge to do.
        ancestor = (conflicts.declared_ancestor(profile.get("merges", []),
                                                heal.REGULATION, lock)
                    if len(reg_contributors) > 1 else None)
        if live is not None:
            bases = heal.prepare_rebase(game, live, ancestor, reg_contributors)
        if bases:
            r.info(f"rebasing merges onto the installed build {live.app}")
        # A declared merge's sources must be faithful before we let resolve()
        # near them: resolve() strips the merged path out of every contributor
        # once a merge succeeds, so a mod that isn't in reinstalled_packages
        # this run may already be missing that content from an earlier merge,
        # with no way to tell that apart from "never collided" just by
        # looking at what's on disk. Checked here, before resolve(), so a
        # stale contributor never gets a chance to silently produce a
        # merge (or a skipped one) built on missing content.
        conflicts.require_faithful_merge_sources(
            profile.get("merges", []), package_ids, reinstalled_packages)
        merge_notes = []
        merged = conflicts.resolve(ME3_DIR, package_ids, profile.get("merges", []),
                                   lock=lock, notes=merge_notes, bases=bases)
        if bases:
            # Prove the rebase did what it claims before anything mounts it. By
            # now the merged file is the only copy of every contributor's rows,
            # and both ways it can go wrong are invisible from the outside: a
            # merge that came out on the mods' old build reverts the patch's
            # game data, and a dropped row shows up in-game only as something
            # that never happens.
            if heal.REGULATION not in merged:
                raise heal.HealError(
                    f"nothing merged {heal.REGULATION}, but this run rebased it "
                    f"onto build {live.regulation} — the packages providing it "
                    f"changed underneath the apply; re-run it")
            problems = heal.verify_rebase(
                (ME3_DIR / "mods" / conflicts.MERGED_ID / heal.REGULATION).read_bytes(),
                bases[heal.REGULATION], reg_contributors, live)
            if problems:
                raise heal.HealError(
                    f"the merged {heal.REGULATION} is not a faithful rebase onto "
                    f"build {live.regulation}:\n  " + "\n  ".join(problems) +
                    f"\nNothing was mounted — fix the mods and re-run `erm apply`.")
    except BaseException:
        # Not just ErmError: apply_renames/apply_prunes move real files, so
        # ENOSPC or EACCES lands here too, and an interrupt at the wrong moment
        # leaves the same inconsistency. Cleanup only -- it re-raises, so the
        # error the caller needs still gets out.
        _persist(state, game, r)
        raise
    declared_mods = {m["path"]: list(m["mods"]) for m in profile.get("merges", [])}
    merged_paths = dict(carried)
    # What actually contributed, not what the profile declared: a merge can name
    # a mod that lives in another profile and isn't installed here, and
    # regulation.bin runs to six contributors. This is the set the NEXT apply
    # tests against installed_packages before carrying the output across, so a
    # declared-but-absent id recorded here is one no later run can ever satisfy
    # -- it would wipe merged output that nothing is going to rebuild.
    merged_paths.update({rel: [m for m in declared_mods.get(rel, []) if m in package_ids]
                         for rel in merged})
    if merged_paths:
        state_mod.record_merged(state, f"tools/me3/mods/{conflicts.MERGED_ID}",
                                merged_paths)
        for rel in merged:
            r.ok(f"merged {rel} (content from {len(merged_paths[rel])} mods kept)")
        # Something a strategy couldn't carry over cleanly -- e.g. two mods'
        # ESD edits landing on the same state machine differently. The merge
        # still happened and one mod's package is still installed; this is the
        # only place that names what the *other* one lost.
        for rel, note in merge_notes:
            r.warn(f"{rel}: {merge.describe_note(note)}")
        for rel in carried:
            r.info(f"kept merged {rel} (declared by another profile)")
    # Stamp the build this stack was applied against, where the rest of the
    # install state is persisted. Without it nothing can tell that the game
    # moved underneath the merged files.
    try:
        state_mod.record_build(state, gamebuild.identify(game, steam_root))
    except GameBuildError as exc:
        r.warn(f"could not record the game build ({exc}) — "
               "a later game patch won't be detected")
    _persist(state, game, r)
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
    return _emit(args, r, doctor=run_doctor(game, Report()))


def cmd_update(args):
    lock_path = Path("mods.lock.toml")
    before = {k: v.get("version") for k, v in manifest.load_lock(lock_path).items()}
    # One lockfile serves every profile, so it always holds more entries than
    # the profile being updated. Report on what this run actually visited --
    # walking `after` instead vouched for mods erm never contacted.
    outcomes = {}
    # One report for the whole run: the fetch's own lines land in it first, then
    # the version diff below. Streams in human mode so an update that pulls
    # hundreds of megabytes isn't silent until it finishes.
    r = Report(stream=not getattr(args, "json", False))
    fetch_profile(args.profile, Path("vendor"), lock_path, update=True, outcomes=outcomes,
                  report=r)
    after = manifest.load_lock(lock_path)

    changed = []
    skipped = []
    for mod_id, outcome in outcomes.items():
        old, new = before.get(mod_id), (after.get(mod_id) or {}).get("version")
        if outcome == "skipped":
            # The pin is unchanged because nothing looked, which is not the
            # same claim as "current" -- warn so it can't read as a clean bill.
            skipped.append(mod_id)
            r.warn(f"{mod_id} pin kept ({new}) — NOT checked against upstream")
        elif old != new:
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

    # The closing summary is report items too, so --json carries the LOCKSTEP
    # warning and the unchecked-pin list instead of trailing them as prose that
    # breaks the document.
    if changed:
        if installed_version:
            r.ok(f"installed seamless-coop {installed_version} into the game")
        r.warn("LOCKSTEP: every player must update to the same version and use the shared "
               "mods.lock.toml, or co-op won't connect. Commit and share the updated lockfile.")
    elif skipped:
        r.info(f"nothing new to install, but {len(skipped)} mod(s) were not checked: "
               f"{', '.join(sorted(skipped))}. Their pins are unverified.")
    else:
        r.ok("already up to date — nothing to install")
    return _emit(args, r, doctor=doctor_report)


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
        pkg_str = entry.get("package")
        if not pkg_str:
            # Same warn-and-forget its two sibling kinds do. Reading the key
            # outright raised KeyError out of every caller instead, and `not`
            # rather than `is None` also catches "", which would become Path(".")
            # and get refused below under the cwd's name.
            r.warn(f"{mod_id}: me3-package entry has no recorded package path — forgetting it")
            state_mod.forget(state, mod_id)
            return
        pkg = Path(pkg_str)
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


# Install modes that leave something behind, mapped to the `kind` their recorder
# writes. Modes absent here ("manual" never installs, "me3-host" installs outside
# the repo) have nothing to reconcile.
_RECORDED_KIND_FOR_INSTALL = {
    "mods": "files",
    "game": "files",
    "me3-native": "me3-native",
    "me3-package": "me3-package",
    "randomizer": "randomizer",
}


def _recorded_kind(entry):
    """Which recorder wrote `entry`, or None if it holds nothing to go on.

    Readable from any entry, including ones written before record_install grew
    its `install` argument: a plain file list is a files-based install whoever
    wrote it, and the other recorders each stamp their own `kind`.
    """
    kind = entry.get("kind")
    if kind in ("me3-native", "me3-package", "randomizer"):
        return kind
    if entry.get("files") is not None:
        return "files"
    return None


def _drop_stale_install(game, mod_id, install_kind, state, r):
    """Remove a mod's previous install when its profile `install` mode changed.

    Every mode records a different entry shape and the recorders overwrite
    state[mod_id] wholesale, so without this the old mode's files stay on disk
    tracked by nothing. For a DLL moving from "mods" to "me3-native" that means
    Elden Mod Loader keeps loading Game/mods/<x>.dll while me3 chainloads the
    native copy — the same mod injected twice, which `erm tidy` won't clean up
    either (it treats .dll as content, not cruft).

    Returns True if the mod is safe to install.
    """
    entry = state.get(mod_id)
    if not entry:
        return True
    want = _RECORDED_KIND_FOR_INSTALL.get(install_kind)
    if want is None:
        return True
    got = _recorded_kind(entry)
    if got is None:
        return True
    was = entry.get("install") or got
    if got == want and (was == install_kind or not entry.get("install")):
        # Same recorder, and either the same mode or an entry too old to name
        # its mode. The only pair this lets through is game <-> mods on a
        # pre-`install` entry, and those both extract into Game/ — the new
        # install lands on top of the old files rather than beside them.
        return True
    try:
        _uninstall_one(game, mod_id, state, r)
    except ErmError as exc:
        # Installing the new mode anyway would leave the old copy live and
        # untracked, which is the exact double-load this guard exists to stop.
        r.warn(f"{mod_id}: install mode changed {was} → {install_kind} but the previous "
               f"copy couldn't be removed ({exc}) — skipping. Uninstall it by hand, "
               f"then re-apply.")
        return False
    r.info(f"{mod_id}: install mode changed {was} → {install_kind} — removed the previous copy")
    return True


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
    # Merged output is derived from packages that just changed, and the merge
    # stripped the merged path out of its sources — so a surviving _merged would
    # be the only provider of content whose source is gone. Drop it; the next
    # apply rebuilds it. Only warn/inform when there was actually something to
    # clear: if the target we just uninstalled WAS _merged itself, the
    # kind == "me3-package" branch in _uninstall_one already removed it and
    # forgot its state entry above, so both checks below are already false and
    # we stay quiet rather than reporting the same removal twice.
    merged_dir = ME3_DIR / "mods" / conflicts.MERGED_ID
    had_merged = merged_dir.is_dir() or conflicts.MERGED_ID in state
    if had_merged:
        try:
            conflicts.clear_merged(ME3_DIR)
        except OSError as exc:
            # clear_merged's rmtree passes ignore_errors=True, so this can't
            # actually raise in production -- the except only fires here
            # because a test replaces shutil.rmtree wholesale. Warn and keep
            # going rather than abort a run that already removed what it came
            # here to remove.
            r.warn(f"could not clear stale merged output ({exc}) — remove tools/me3/mods/_merged by hand")
        else:
            r.info("cleared merged output — run `erm apply` to rebuild it")
        state_mod.forget(state, conflicts.MERGED_ID)
    state_mod.write_state(Path("installed.json"), state)
    try:
        me3profile.reconcile(state, ME3_DIR, game)
    except OSError as exc:
        r.warn(f"could not regenerate the me3 profile ({exc}) — run `erm apply` again")
    return _emit(args, r, doctor=run_doctor(game, Report()))


def cmd_switch(args):
    """Uninstall every mod currently recorded as installed, then apply a new
    profile — the clean way to move between mod stacks without leftovers
    from the old one lingering in Game/."""
    game = paths.find_game_dir(paths.find_steam_root())
    state = state_mod.load_state()
    r = Report(stream=not getattr(args, "json", False))
    # mod_ids, not every key: installed.json also holds bookkeeping records
    # like the build stamp, and handing one to the uninstaller raises PathError
    # — whose recovery is to forget the entry, which deletes the stamp.
    for mid in state_mod.mod_ids(state):
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
    # Same report through the apply half, so a switch is one report and one
    # --json document rather than the uninstall's, then apply's, then doctor's.
    return cmd_apply(type("A", (), {
        "profile": args.profile,
        "json": args.json,
        "no_harden": getattr(args, "no_harden", False),
    })(), r)


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
    return _emit(args, r)


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def cmd_backup(args):
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    r = Report()
    co2 = list(save_dir.glob("*.co2")) or list(save_dir.glob("*.sl2"))
    if not co2:
        r.fail("no save found to back up")
        return _emit(args, r)
    out = saves.backup_save(co2[0], BACKUPS_DIR, label=args.label or "", stamp=_stamp())
    r.ok(f"backed up {co2[0].name} → {out}")
    return _emit(args, r)


def cmd_quarantine(args):
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    sl2 = save_dir / "ER0000.sl2"
    rep = saves.quarantine(sl2, BACKUPS_DIR, steam.cloud_saves(root),
                           steam_up=steam.steam_running(), stamp=_stamp())
    return _emit(args, rep)


def _backup_names():
    """Snapshot names as `erm restore` takes them — relative to backups/, so a
    quarantined save reads `quarantine/ER0000.sl2.<stamp>` and pastes straight
    back into the command."""
    return [str(p.relative_to(BACKUPS_DIR)) for p in saves.list_backups(BACKUPS_DIR)]


def cmd_backups(args):
    r = Report()
    names = _backup_names()
    if not names:
        r.info(f"no snapshots in {BACKUPS_DIR}/ — `erm backup` takes one")
    for name in names:
        r.info(name)
    return _emit(args, r)


def cmd_restore(args):
    src = BACKUPS_DIR / args.backup
    if not src.exists():
        src = Path(args.backup)
    if not src.exists():
        # Before the pre-restore snapshot below, not after: a mistyped name
        # used to copy the live save into backups/ and only then fail, leaving
        # another file behind in the directory that nothing could list.
        names = _backup_names()
        raise PathError(f"no backup named {args.backup!r} "
                        f"({'have: ' + ', '.join(names) if names else 'backups/ is empty'})")
    root = paths.find_steam_root()
    save_dir = paths.find_save_dir(root)
    dest = save_dir / ("ER0000.co2" if src.name.endswith(".co2") or ".co2" in src.name else "ER0000.sl2")
    r = Report()
    if dest.exists():
        # The save being overwritten is a live character, so where its last
        # copy went is part of the result, not a detail worth losing.
        kept = saves.backup_save(dest, BACKUPS_DIR, label="pre-restore", stamp=_stamp())
        r.info(f"kept the save being replaced → {kept}")
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        raise PathError(f"cannot restore from {src} ({exc})") from exc
    r.ok(f"restored {src} → {dest}")
    return _emit(args, r)


def cmd_tidy(args):
    """Remove runtime cruft `erm uninstall`/`switch` leave behind that
    installed.json never tracked — per-mod log dirs, ERSC crash dumps, loader
    logs. Dry-run by default (lists candidates); `--apply` actually deletes.
    Every candidate already passed find_cruft's containment/recorded/critical
    checks, so this loop just acts on what it's handed."""
    game = paths.find_game_dir(paths.find_steam_root())
    state = state_mod.load_state()
    recorded = {rel for mid in state_mod.mod_ids(state)
                for rel in state[mid].get("files", [])}
    cruft = tidy.find_cruft(game, recorded)
    r = Report()
    if not cruft:
        r.ok("nothing to tidy — no orphaned mod logs/dirs found")
        return _emit(args, r)
    verb = "removing" if args.apply else "would remove"
    for c in cruft:
        r.info(f"{verb}: {c.relative_to(game)}")
    if not args.apply:
        r.info(f"{len(cruft)} item(s) would be removed (all inside Game/, none recorded in "
               f"installed.json). Re-run `erm tidy --apply` to delete them.")
        return _emit(args, r)
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
    return _emit(args, r)


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
    return _emit(args, r, doctor=run_doctor(game, Report()))


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
    return _emit(args, r, doctor=run_doctor(game, Report()))


def register(subparsers):
    # --json on every subcommand as well as the root, so `erm status --json`
    # works like git/docker rather than exiting 2. SUPPRESS is load-bearing: a
    # plain store_true default would copy False back over the root's True and
    # silently turn `erm --json status` into prose.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")

    def add(name, **kw):
        return subparsers.add_parser(name, parents=[common], **kw)

    add("doctor", help="safety report").set_defaults(func=cmd_doctor)
    a = add("audit", help="forensic audit of a save")
    a.add_argument("save", nargs="?", help="path to ER0000.sl2 (default: live save)")
    a.set_defaults(func=cmd_audit)
    add("status", help="install + version summary").set_defaults(func=cmd_status)
    add(
        "launch-option", help="print every Steam launch option"
    ).set_defaults(func=cmd_launch_option)
    f = add("fetch", help="download + verify a profile's mods")
    f.add_argument("profile", nargs="?", default="seamless-only")
    f.add_argument("--update", action="store_true",
                    help="ignore the lockfile pin and fetch the latest release")
    f.set_defaults(func=cmd_fetch)
    ap = add("apply", help="install the fetched mods into Game/")
    ap.add_argument("profile", nargs="?", default="seamless-only")
    ap.add_argument("--no-harden", action="store_true",
                     help="skip auto-harden even if the profile loads mods via a proxy DLL/me3")
    ap.set_defaults(func=cmd_apply)
    up = add("update", help="fetch the latest Seamless Co-op, re-pin, and install it")
    up.add_argument("profile", nargs="?", default="seamless-only")
    up.set_defaults(func=cmd_update)
    un = add("uninstall", help="remove an installed mod's (or whole profile's) files from Game/")
    un.add_argument("mod", nargs="?", default="seamless-coop")
    un.set_defaults(func=cmd_uninstall)
    sw = add("switch", help="uninstall whatever's installed, then apply a different profile")
    sw.add_argument("profile")
    sw.add_argument("--no-harden", action="store_true",
                     help="skip auto-harden even if the new profile loads mods via a proxy DLL/me3")
    sw.set_defaults(func=cmd_switch)
    add("verify", help="re-hash vendor/ against the lockfile").set_defaults(func=cmd_verify)
    p_refresh = add(
        "refresh",
        help="show what the installed game build needs rebased onto it — `erm apply` carries it out")
    # Neither flag gates a write: refresh reports, `erm apply` carries it out.
    # They shape the printed plan, and the help has to say only that.
    p_refresh.add_argument("--dry-run", action="store_true",
                           help="print the plan alone, without the command that carries it out")
    p_refresh.add_argument("--no-reharden", action="store_true",
                           help="leave the launcher re-copy out of the plan "
                                "(a stale launcher is still reported)")
    p_refresh.set_defaults(func=cmd_refresh)
    b = add("backup", help="snapshot the co-op save")
    b.add_argument("--label", default="")
    b.set_defaults(func=cmd_backup)
    add("backups", help="list the save snapshots `erm restore` can take").set_defaults(
        func=cmd_backups)
    rs = add("restore", help="restore a save snapshot")
    rs.add_argument("backup", help="a name from `erm backups`, or a path to a file")
    rs.set_defaults(func=cmd_restore)
    add("quarantine", help="move the vanilla save out of harm's way").set_defaults(func=cmd_quarantine)
    add(
        "harden",
        help="swap in a non-EAC launcher and lock it immutable (sudo)",
    ).set_defaults(func=cmd_harden)
    add(
        "unharden",
        help="undo `erm harden`: restore the real EAC launcher (sudo)",
    ).set_defaults(func=cmd_unharden)
    td = add(
        "tidy",
        help="remove orphaned mod logs/runtime dirs left behind by uninstall (dry-run by default)",
    )
    td.add_argument("--apply", action="store_true",
                     help="actually delete the candidates (default: list only)")
    td.set_defaults(func=cmd_tidy)
