import zipfile
import pytest
from pathlib import Path
from ermlib.me3pkg import find_package_root, install_me3_package
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
