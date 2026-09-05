"""Install ERSC into Game/ and re-inject the co-op password.

An ERSC update ships its own ersc_settings.ini with cooppassword blank, so
apply() always has to rewrite it after extracting — otherwise the whole
group silently loses connection until someone notices.
"""
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from .errors import ErmError
from .paths import is_safe_relpath


def read_secret(env_path, key="COOP_PASSWORD"):
    for line in Path(env_path).read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def inject_password(settings_ini, password):
    ini = Path(settings_ini)
    text = ini.read_text()
    if "@COOP_PASSWORD@" in text:
        text = text.replace("@COOP_PASSWORD@", password)
    else:
        text, hits = re.subn(r"(?m)^cooppassword\s*=.*$",
                             f"cooppassword = {password}", text)
        # An ERSC release that renames the key would otherwise leave re.subn
        # matching nothing and this function writing the file back unchanged --
        # a session with a blank password that anyone can join, reported as a
        # success. Only worth refusing when a password was actually supplied:
        # having none is a state apply already warns about.
        if not hits and password:
            raise ErmError(
                f"{ini}: no `cooppassword` line to write the co-op password "
                f"into — the settings layout changed, so erm can't set it. "
                f"Set it by hand in the file and re-run.")
    ini.write_text(text)


EXTRACTOR = "bsdtar"


def _require_extractor(archive):
    exe = shutil.which(EXTRACTOR)
    if exe is None:
        # BadZipFile, not ErmError: apply catches this per-mod and keeps going.
        # An ErmError aborts the whole run before write_state, which would throw
        # away the install record for every mod that already succeeded. Same
        # reasoning for the unreadable-archive failures below — only a hostile
        # path is worth aborting for.
        raise zipfile.BadZipFile(
            f"{Path(archive).name} is not a zip and {EXTRACTOR} isn't installed — "
            f"install {EXTRACTOR} (libarchive) to handle .rar/.7z mod archives, "
            f"or extract it by hand")
    return exe


def _list_archive(archive):
    """Member names inside a non-zip archive, via libarchive."""
    exe = _require_extractor(archive)
    try:
        out = subprocess.run([exe, "-tf", str(archive)],
                             check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        raise zipfile.BadZipFile(
            f"{Path(archive).name} is not a readable archive: "
            f"{str(detail).strip()}") from exc
    return [n for n in out.splitlines() if n]


def _extract_other(archive, dest, names):
    exe = _require_extractor(archive)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([exe, "-xf", str(archive), "-C", str(dest)],
                       check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        raise zipfile.BadZipFile(
            f"cannot extract {Path(archive).name}: {str(detail).strip()}") from exc
    # bsdtar lists directories without a trailing slash, unlike zipfile, so
    # filter on what actually landed rather than on the name's shape.
    return [n for n in names if (dest / n).is_file()]


def _tar_names(archive):
    """Member names inside a tarball, normalized to plain relative paths.

    me3 writes its members as "./bin/me3"; the leading "./" is noise that would
    otherwise show up in installed.json and in every uninstall comparison.
    """
    with tarfile.open(archive) as tf:
        names = []
        for m in tf.getmembers():
            if not m.isfile():
                continue
            names.append(m.name[2:] if m.name.startswith("./") else m.name)
        return names


def _extract_tar(archive, dest, names):
    if not hasattr(tarfile, "data_filter"):
        # The extraction filter landed in 3.11.4 / 3.12 (and distros backport
        # it into older 3.11 builds), so probe the capability rather than the
        # version number. Without it extractall rejects the `filter` kwarg and
        # the TypeError escapes apply's per-mod handler, killing the run before
        # write_state — mod files in Game/ that installed.json never recorded.
        # BadZipFile for the same reason as the missing extractor above: skip
        # this archive, keep the run.
        raise zipfile.BadZipFile(
            f"{Path(archive).name} needs Python 3.11.4+ to extract safely — "
            f"this interpreter's tarfile has no extraction filter. Upgrade "
            f"Python, or unpack it into the destination by hand")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        # `data` refuses absolute paths, `..` escapes, links out of the tree and
        # device nodes. The name guard above already ran; this closes the gap
        # between a member's name and what extraction actually writes.
        tf.extractall(dest, filter="data")
    return [n for n in names if (dest / n).is_file()]


def _extract_zip(z, dest, names):
    dest.mkdir(parents=True, exist_ok=True)
    z.extractall(dest)
    return [n for n in names if not n.endswith("/")]


def _wrapper_dir(names):
    """The single top-level directory every member sits under, or None.

    Nexus DLL mods are routinely zipped inside one folder named after the mod
    and its version. Elden Mod Loader only scans mods/*.dll, so that folder has
    to come off or the DLL ends up one level too deep and simply never loads.
    A lone top-level FILE is not a wrapper (NoWeight.dll ships bare).
    """
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) != 1:
        return None
    top = tops.pop()
    # Every member must be *inside* it — if the archive is one bare file, its
    # own name is the only "top" and there's nothing to strip.
    if all(n == top or not n.startswith(f"{top}/") for n in names):
        return None
    return top


def extract_archive(archive_path, dest_root, dest_subdir="", strip_wrapper=False):
    """Extract an archive into dest_root/dest_subdir, rejecting unsafe members.

    Handles zip natively and anything else (.rar, .7z — both common on Nexus)
    through libarchive.

    With strip_wrapper, a single enclosing directory is removed so the archive's
    real contents land directly in the destination. Off by default: an
    install="game" archive usually IS a single top-level `mods/` dir, and
    stripping that would drop its DLLs into Game/ where nothing loads them.

    Returns the list of extracted files as paths RELATIVE TO dest_root (so
    dest_subdir is prefixed onto every entry) — that's what installed.json
    records and what `erm uninstall` later removes.
    """
    dest_root = Path(dest_root)
    dest = dest_root / dest_subdir if dest_subdir else dest_root
    try:
        z = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile:
        z = None
    try:
        if z is None and tarfile.is_tarfile(archive_path):
            # Read tarballs with the stdlib rather than libarchive: me3 ships its
            # Linux build as .tar.gz, and making the loader's own install depend on
            # bsdtar being on PATH is how it silently fails to update.
            src, names, extract = archive_path, _tar_names(archive_path), _extract_tar
        elif z is None:
            src, names, extract = archive_path, _list_archive(archive_path), _extract_other
        else:
            src, names, extract = z, z.namelist(), _extract_zip
        # Zip-slip guard, every format: a trojaned archive could name a member
        # ../../../etc/x and have extraction write outside dest_root. The sha256
        # pin proves the archive is the chosen one, not that it's benign. Reject
        # any absolute path or one with a `..` component BEFORE extracting
        # anything, so a bad archive is never partially written — which is also
        # why this sits between listing and extraction rather than inside each
        # branch, where a fourth format could quietly skip it. The returned list
        # is then guaranteed safe relative paths.
        for name in names:
            if not is_safe_relpath(name):
                raise ErmError(f"unsafe path in mod archive (refusing to install): {name}")
        rels = extract(src, dest, names)
    finally:
        if z is not None:
            z.close()
    wrapper = _wrapper_dir(names) if strip_wrapper else None
    if wrapper:
        rels = _strip_wrapper_dir(dest, wrapper, rels)
    return [(f"{dest_subdir}/{n}" if dest_subdir else n) for n in rels]


def _strip_wrapper_dir(dest, wrapper, rels):
    """Move everything out of dest/<wrapper> up into dest, then drop it."""
    wdir = dest / wrapper
    for child in list(wdir.iterdir()):
        target = dest / child.name
        # Re-applying a profile extracts over a previous install; move refuses
        # to replace an existing dir, so clear the way first.
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        shutil.move(str(child), str(target))
    shutil.rmtree(wdir, ignore_errors=True)
    return [n[len(wrapper) + 1:] for n in rels if n.startswith(f"{wrapper}/")]


def apply_ersc(zip_path, game_dir, password):
    """Extract the ERSC archive into game_dir and re-inject the password.

    Returns the list of real files it wrote (relative POSIX paths under
    game_dir, directories excluded) — `erm uninstall` records this so it
    knows exactly what to remove later.
    """
    game_dir = Path(game_dir)
    legacy = game_dir / "launch_elden_ring_seamlesscoop.exe"
    if legacy.exists():
        legacy.unlink()
    sc = game_dir / "SeamlessCoop"
    if sc.exists():
        shutil.rmtree(sc)
    files = extract_archive(zip_path, game_dir, "")
    inject_password(game_dir / "SeamlessCoop" / "ersc_settings.ini", password)
    return files
