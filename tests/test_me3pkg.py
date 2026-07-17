import zipfile
import pytest
from pathlib import Path
from ermlib.me3pkg import find_package_root, install_me3_package, list_option_dirs
from ermlib.errors import PathError


def _tree(base, *rels):
    for r in rels:
        p = base / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_bare_tree_at_root(tmp_path):
    _tree(tmp_path, "parts/wp_a.dcx", "menu/hud.dds")
    assert find_package_root(tmp_path) == tmp_path


def test_single_wrapper_folder_is_descended(tmp_path):
    _tree(tmp_path, "My Cool Mod/parts/wp_a.dcx")
    assert find_package_root(tmp_path) == tmp_path / "My Cool Mod"


def test_modengine_mod_folder_convention(tmp_path):
    _tree(tmp_path, "mod/chr/c0000.dcx")
    assert find_package_root(tmp_path) == tmp_path / "mod"


def test_tree_beside_a_readme(tmp_path):
    _tree(tmp_path, "parts/wp_a.dcx", "README.txt", "preview.png")
    assert find_package_root(tmp_path) == tmp_path


def test_modengine2_launcher_files_dont_block_descent(tmp_path):
    _tree(tmp_path, "modengine2_launcher.exe", "config_eldenring.toml", "mod/parts/wp.dcx")
    assert find_package_root(tmp_path) == tmp_path / "mod"


def test_no_asset_dirs_returns_none(tmp_path):
    _tree(tmp_path, "random/thing.bin", "notes.docx")
    assert find_package_root(tmp_path) is None


def test_regulation_only_is_a_valid_root(tmp_path):
    (tmp_path / "regulation.bin").write_bytes(b"x")
    assert find_package_root(tmp_path) == tmp_path


def test_regulation_bin_is_matched_case_insensitively(tmp_path):
    (tmp_path / "Regulation.BIN").write_bytes(b"x")
    assert find_package_root(tmp_path) == tmp_path


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def test_install_places_normalized_tree(tmp_path):
    arc = tmp_path / "TexPack.zip"
    _zip(arc, {"TexPack/parts/wp.dcx": "a", "TexPack/asset/x.dcx": "b", "TexPack/readme.txt": "hi"})
    me3_dir = tmp_path / "tools" / "me3"
    pkg, has_reg = install_me3_package(arc, "texture-improvement", me3_dir)
    assert pkg == str(me3_dir / "mods" / "texture-improvement")
    # the wrapper folder is stripped: parts/ sits at the package root, not under TexPack/
    assert (me3_dir / "mods" / "texture-improvement" / "parts" / "wp.dcx").is_file()
    assert (me3_dir / "mods" / "texture-improvement" / "asset" / "x.dcx").is_file()
    assert has_reg is False


def test_install_flags_regulation_bin(tmp_path):
    arc = tmp_path / "Shared.zip"
    _zip(arc, {"regulation.bin": "r", "param/x.dcx": "p"})
    me3_dir = tmp_path / "tools" / "me3"
    _, has_reg = install_me3_package(arc, "some-mod", me3_dir)
    assert has_reg is True


def test_install_raises_when_no_asset_root(tmp_path):
    arc = tmp_path / "Weird.zip"
    _zip(arc, {"docs/notes.docx": "n"})
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError):
        install_me3_package(arc, "weird", me3_dir)


# Some Nexus mods (e.g. Minimal HUD #148) ship multiple complete variant
# folders at the archive root — find_package_root correctly refuses to guess
# between them, so a profile entry can name the one to use via `subdir`.
def _option_folders_zip(path):
    _zip(path, {
        "OPTION 1 - Normal Backgrounds/menu/x.gfx": "a",
        "OPTION 2 - Translucent Backgrounds/menu/y.gfx": "b",
    })


def test_install_uses_named_subdir_option_folder(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    pkg, has_reg = install_me3_package(arc, "minimal-hud", me3_dir,
                                        subdir="OPTION 1 - Normal Backgrounds")
    dest = me3_dir / "mods" / "minimal-hud"
    assert pkg == str(dest)
    assert (dest / "menu" / "x.gfx").is_file()
    # the other option's file must not leak into the placed package
    assert not (dest / "menu" / "y.gfx").exists()
    assert has_reg is False


def test_install_without_subdir_raises_on_multiple_option_folders(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError):
        install_me3_package(arc, "minimal-hud", me3_dir)


def test_install_subdir_missing_raises(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError):
        install_me3_package(arc, "minimal-hud", me3_dir, subdir="does-not-exist")


def test_install_subdir_rejects_path_escape(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError):
        install_me3_package(arc, "minimal-hud", me3_dir, subdir="../escape")


def test_install_subdir_rejects_absolute_path(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError):
        install_me3_package(arc, "minimal-hud", me3_dir, subdir="/etc")


def test_list_option_dirs_finds_each_self_contained_option(tmp_path):
    _tree(tmp_path, "OPTION 1/menu/x.gfx", "OPTION 2/menu/y.gfx")
    assert list_option_dirs(tmp_path) == ["OPTION 1", "OPTION 2"]


def test_list_option_dirs_empty_for_a_normal_single_root_tree(tmp_path):
    # A bare asset dir at the root is a single clear package root, not a set
    # of options — list_option_dirs must not misreport it as one.
    _tree(tmp_path, "parts/x.dcx")
    assert list_option_dirs(tmp_path) == []


def test_install_without_subdir_lists_option_names_in_error(tmp_path):
    arc = tmp_path / "MinimalHud.zip"
    _option_folders_zip(arc)
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError) as exc:
        install_me3_package(arc, "minimal-hud", me3_dir)
    msg = str(exc.value)
    assert "OPTION 1 - Normal Backgrounds" in msg
    assert "OPTION 2 - Translucent Backgrounds" in msg


def test_install_without_subdir_wording_is_count_agnostic_for_a_single_option(tmp_path):
    # A stray file at the staging root can disqualify the root itself as a
    # package (find_package_root refuses to descend past it) even though
    # exactly one subdir underneath qualifies on its own — list_option_dirs
    # then reports a single option, not multiple. The error message must not
    # claim "multiple option folders" in that case.
    arc = tmp_path / "SingleOption.zip"
    _zip(arc, {
        "somefile.dat": "stray, not a doc ext — disqualifies the root",
        "ModFolder/parts/wp.dcx": "a",
    })
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError) as exc:
        install_me3_package(arc, "single-option", me3_dir)
    msg = str(exc.value)
    assert "multiple" not in msg.lower()
    assert "ModFolder" in msg


def test_install_unrecognizable_archive_keeps_generic_message(tmp_path):
    arc = tmp_path / "Weird.zip"
    _zip(arc, {"docs/notes.docx": "n"})
    me3_dir = tmp_path / "tools" / "me3"
    with pytest.raises(PathError) as exc:
        install_me3_package(arc, "weird", me3_dir)
    msg = str(exc.value)
    assert "option" not in msg.lower()
