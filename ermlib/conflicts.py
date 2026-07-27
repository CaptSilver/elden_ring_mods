"""Find and resolve files that two me3 packages both provide.

me3 maps one file per game-relative path and picks a winner silently -- it
doesn't even warn (me3 issue #44, open since 2025). So a collision means one
mod's content just isn't in the game, with no symptom unless you happen to
notice missing text. Detecting that is the point of this module; merging is
what we do about the one case we can resolve.
"""
import shutil
import zipfile
from pathlib import Path

from .errors import ErmError
from .merge import NEEDS_VANILLA, STRATEGIES
from .paths import is_safe_relpath

MERGED_ID = "_merged"


class ConflictError(ErmError):
    """Two mods provide the same file and nothing says how to resolve it."""


def _package_dir(me3_dir, mod_id):
    return Path(me3_dir) / "mods" / mod_id


def index_paths(me3_dir, mod_ids):
    """Map each game-relative path to the mods providing it, in `mod_ids` order."""
    index = {}
    for mod_id in mod_ids:
        root = _package_dir(me3_dir, mod_id)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                index.setdefault(path.relative_to(root).as_posix(), []).append(mod_id)
    return index


def apply_prunes(me3_dir, prunes):
    """Delete author-declared dead files. Returns the "mod:path" strings removed.

    A missing file is not an error: the mod may have stopped shipping it, which
    is the outcome the prune was asking for.
    """
    removed = []
    for entry in prunes:
        mod_id = entry["mod"]
        for rel in entry["paths"]:
            # `rel` comes straight out of a profile TOML, not the filesystem --
            # unlike index_paths's rglob results, nothing has confirmed it stays
            # under the package dir. A stray leading `../` (typo or otherwise)
            # would otherwise delete a file elsewhere on disk.
            if not is_safe_relpath(rel):
                raise ConflictError(f"unsafe prune path (refusing to delete): {rel}")
            target = _package_dir(me3_dir, mod_id) / rel
            if target.is_file():
                target.unlink()
                removed.append(f"{mod_id}:{rel}")
    return removed


def _declare_merges(merges):
    """Index merges by path, refusing to silently pick one of two conflicting
    declarations for it.

    Profiles compose, so the same path can arrive here with a merge declared
    by two different included profiles. A plain `{m["path"]: m for m in
    merges}` would keep whichever entry happens to be last and drop the
    other's strategy/prefer/mods without telling anyone -- the exact kind of
    silent surprise this module exists to catch.
    """
    declared = {}
    for m in merges:
        rel = m["path"]
        prior = declared.get(rel)
        if prior is not None and prior != m:
            raise ConflictError(
                f"{rel}: two [[merges]] entries disagree on how to resolve it "
                f"-- {prior!r} vs {m!r}. Profiles compose, so this path picked "
                f"up conflicting declarations from different includes; edit "
                f"one so they agree.")
        declared[rel] = m
    return declared


def _check_no_case_only_collisions(index):
    """Refuse paths that collide only under case-folding.

    Whether me3's runtime path resolution is case-sensitive is unverified, so
    we don't normalize or guess which of two differently-cased paths it would
    actually mount -- we refuse and make the profile author resolve it. This
    also catches collisions the provider-count check below cannot see at all:
    `msg/x.dcx` and `MSG/X.dcx` index as two *different* one-provider entries,
    so nothing else in this module ever notices they name the same me3 slot.
    """
    by_fold = {}
    for rel in index:
        by_fold.setdefault(rel.casefold(), []).append(rel)
    for variants in by_fold.values():
        if len(variants) > 1:
            detail = "; ".join(
                f"{v!r} (from {', '.join(index[v])})" for v in sorted(variants))
            raise ConflictError(
                f"these paths differ only in case, so it's ambiguous which "
                f"file me3 would mount: {detail} -- rename one so the paths "
                f"differ for real, or drop one of the mods")


def require_faithful_merge_sources(merges, present_ids, reinstalled_ids):
    """Refuse a merge whose contributors aren't known-faithful sources this run.

    resolve() strips the merged path out of every contributor once a merge
    succeeds, so a package that's only sitting in state because an EARLIER
    apply put it there -- not because this run actually (re)installed it --
    may already be missing that content. There's no way to tell that apart
    from a package that's simply never collided before by looking at
    index_paths()'s output alone, so the caller (apply) has to track which
    ids it actually re-extracted this run and hand both sets in here, before
    resolve() ever runs.

    A merge with fewer than two of its declared mods currently present isn't
    a real collision regardless -- it's the same "declared mod simply isn't
    installed" case resolve() itself skips (see
    test_merge_is_skipped_when_only_one_side_is_installed) -- so that's left
    alone here too; only a merge that's actually about to run this time is
    at risk.
    """
    present_ids = set(present_ids)
    for entry in merges:
        present = [mid for mid in entry["mods"] if mid in present_ids]
        if len(present) < 2:
            continue
        stale = sorted(mid for mid in present if mid not in reinstalled_ids)
        if stale:
            raise ConflictError(
                f"{entry['path']}: {', '.join(stale)} "
                f"{'is' if len(stale) == 1 else 'are'} on disk only from an "
                f"earlier apply, not freshly (re)installed this run -- likely "
                f"a missing vendor archive or a failed install reported "
                f"above. Recomputing this merge from a package that wasn't "
                f"just re-extracted risks silently losing content an earlier "
                f"merge already stripped out of it. Fix the install for "
                f"{', '.join(stale)} (refetch it / restore its vendor "
                f"archive) and re-run apply.")


def _load_vanilla(rel, spec, lock):
    """Read the vanilla file a three-way merge compares against.

    It comes out of a vendored archive rather than the game install: the game
    keeps its text inside the DVDBND, which ermlib can't open, and a vendored
    archive is already pinned by hash in mods.lock.toml. Reading the zip
    directly rather than an extracted copy under tools/ matters because that
    directory only exists while the owning profile is applied.
    """
    source = spec.get("vanilla")
    if not source:
        raise ConflictError(
            f"{rel}: strategy {spec['strategy']!r} compares both mods against the "
            f"vanilla file they branched from, but no `vanilla` is declared. Add "
            f"vanilla = {{ mod = \"<id>\", member = \"<path inside its archive>\" }}")
    mod_id, member = source.get("mod"), source.get("member")
    if not (mod_id and member):
        raise ConflictError(
            f"{rel}: `vanilla` needs both `mod` and `member`, got {source!r}")
    if not is_safe_relpath(member):
        raise ConflictError(f"{rel}: unsafe vanilla member path {member!r}")
    entry = (lock or {}).get(mod_id)
    asset = entry.get("asset") if entry else None
    if not asset:
        raise ConflictError(
            f"{rel}: vanilla comes from mod {mod_id!r}, which has no archive in "
            f"mods.lock.toml — run `erm fetch` for the profile that pins it")
    archive = Path("vendor") / asset
    if not archive.exists():
        raise ConflictError(f"{rel}: vanilla archive missing from vendor/ ({asset})")
    try:
        with zipfile.ZipFile(archive) as z:
            return z.read(member)
    except KeyError as exc:
        raise ConflictError(
            f"{rel}: {member!r} is not in {asset} — check the member path against "
            f"the archive's actual layout") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConflictError(f"{rel}: cannot read vanilla from {asset}: {exc}") from exc


def resolve(me3_dir, mod_ids, merges, lock=None):
    """Merge every declared conflict and refuse any undeclared one.

    Merged output goes to a synthetic package and the path is removed from its
    sources, so the merged file is the only one providing it. That sidesteps
    me3's load order entirely rather than trusting it to break the tie our way.

    Atomic across the whole call: every merge is computed and held in memory
    first, and only once all of them succeed do we write `_merged` and unlink
    the sources. A strategy raising partway through must leave every package
    exactly as it was found -- otherwise a later path's failure would strand
    an earlier path half-migrated, with its source already deleted and no
    merged output to show for it either.
    """
    me3_dir = Path(me3_dir)
    declared = _declare_merges(merges)
    index = index_paths(me3_dir, mod_ids)
    _check_no_case_only_collisions(index)

    planned = []
    for rel, providers in sorted(index.items()):
        if len(providers) < 2:
            continue
        spec = declared.get(rel)
        if spec is None:
            raise ConflictError(
                f"{rel} is provided by {', '.join(providers)} — me3 loads only one "
                f"of them, so the rest silently won't be in the game. Declare a "
                f"[[merges]] entry for this path, or drop one of the mods.")
        strategy = STRATEGIES.get(spec["strategy"])
        if strategy is None:
            raise ConflictError(
                f"{rel}: unknown merge strategy {spec['strategy']!r} "
                f"(known: {', '.join(sorted(STRATEGIES))})")
        # A mod outside the declared set folding into the merge is unreviewed
        # content silently included -- same class of bug as content silently
        # dropped. A declared mod simply not being *installed* is fine (profiles
        # compose; test_merge_is_skipped_when_only_one_side_is_installed covers
        # the case where that drops providers below 2 entirely) -- so we only
        # object to providers absent from the declaration, never the reverse.
        undeclared = set(providers) - set(spec["mods"])
        if undeclared:
            raise ConflictError(
                f"{rel}: merge declares mods={sorted(spec['mods'])}, but the "
                f"actual providers are {sorted(providers)} -- "
                f"{', '.join(sorted(undeclared))} also ship this path and "
                f"aren't declared. Update the [[merges]] entry to include "
                f"them deliberately, or drop the extra mod.")
        prefer = spec["prefer"]
        if prefer not in providers:
            # A typo'd or stale `prefer` would otherwise reach read_bytes() below
            # and fail as a raw FileNotFoundError pointing at an internal path --
            # correct behind-the-scenes, but useless to whoever wrote the profile.
            raise ConflictError(
                f"{rel}: merge names prefer={prefer!r}, but the actual providers "
                f"are {', '.join(providers)} — prefer must be one of them")
        # `prefer` wins genuine collisions, so it's the base the strategy builds on.
        ordered = [prefer] + [m for m in providers if m != prefer]
        blobs = [(_package_dir(me3_dir, m) / rel).read_bytes() for m in ordered]
        # Three-way strategies need the common ancestor as well. Resolved once
        # per path, and only for strategies that ask for it, so a two-way
        # declaration never has to name a vanilla it wouldn't look at.
        vanilla = _load_vanilla(rel, spec, lock) if spec["strategy"] in NEEDS_VANILLA else None
        out = blobs[0]
        for extra in blobs[1:]:
            out = strategy(out, extra, vanilla) if vanilla is not None else strategy(out, extra)
        planned.append((rel, out, providers))

    # Every merge above succeeded in memory -- only now do we touch disk, so a
    # raise anywhere in the loop above never gets here at all.
    for rel, out, providers in planned:
        target = _package_dir(me3_dir, MERGED_ID) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out)
        for mod_id in providers:
            (_package_dir(me3_dir, mod_id) / rel).unlink()
    return [rel for rel, _, _ in planned]


def clear_merged(me3_dir, keep=()):
    """Delete the synthetic merged package, except for `keep`.

    This alone does not recover anything -- it just clears prior merged
    output so the next resolve() rebuilds it from scratch. The reason that's
    safe to do after a failed resolve() is resolve()'s own atomicity (it never
    unlinks a source until every merge in the run has succeeded), not
    anything this function does.

    `keep` is for merged output the caller is NOT going to rebuild this run.
    A successful merge strips the path from every contributor, so that output
    is the only remaining copy -- wiping it when nothing will regenerate it
    leaves no package providing the file at all, which is silent content loss
    of exactly the kind this module exists to prevent.
    """
    root = _package_dir(me3_dir, MERGED_ID)
    keep = {str(k) for k in keep}
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
        return
    for path in sorted((p for p in root.rglob("*") if p.is_file()), reverse=True):
        if path.relative_to(root).as_posix() not in keep:
            path.unlink()
    # Prune directories the removals emptied, deepest first, but never the
    # package root -- an empty root still means "this package exists".
    for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
