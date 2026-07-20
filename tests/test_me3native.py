import zipfile
import pytest
from pathlib import Path

from ermlib import state as state_mod
from ermlib.me3pkg import install_me3_native, find_native_dll
from ermlib.me3profile import reconcile
from ermlib.errors import PathError


def _zip(path, **members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name.replace("|", "/"), data)
    return path


def _game(tmp_path):
    g = tmp_path / "Game"
    (g / "SeamlessCoop").mkdir(parents=True, exist_ok=True)
    (g / "SeamlessCoop" / "ersc.dll").write_bytes(b"x")
    return g


# --- locating the DLL inside an extracted archive ---

def test_find_native_dll_picks_the_only_dll(tmp_path):
    (tmp_path / "QuestPath").mkdir()
    dll = tmp_path / "QuestPath" / "QuestPath.dll"
    dll.write_bytes(b"MZ")
    (tmp_path / "QuestPath" / "QuestPath.ini").write_text("")
    assert find_native_dll(tmp_path, "questpath") == dll


def test_find_native_dll_prefers_the_name_matching_the_mod_id(tmp_path):
    # Some archives ship a dependency DLL beside the mod's own. Matching on the
    # id keeps us from chainloading the dependency and leaving the mod dormant.
    (tmp_path / "QuestPath.dll").write_bytes(b"MZ")
    (tmp_path / "vcruntime140.dll").write_bytes(b"MZ")
    assert find_native_dll(tmp_path, "questpath") == tmp_path / "QuestPath.dll"


def test_find_native_dll_returns_none_when_ambiguous(tmp_path):
    (tmp_path / "alpha.dll").write_bytes(b"MZ")
    (tmp_path / "beta.dll").write_bytes(b"MZ")
    assert find_native_dll(tmp_path, "questpath") is None


def test_find_native_dll_returns_none_when_absent(tmp_path):
    (tmp_path / "readme.txt").write_text("")
    assert find_native_dll(tmp_path, "questpath") is None


# --- installing ---

def test_install_me3_native_places_dll_under_natives(tmp_path):
    src = _zip(tmp_path / "qp.zip", **{"QuestPath|QuestPath.dll": b"MZ",
                                       "QuestPath|QuestPath.ini": b"[overlay]"})
    me3 = tmp_path / "tools" / "me3"
    dll = install_me3_native(src, "questpath", me3)
    assert Path(dll).exists()
    assert Path(dll).name == "QuestPath.dll"
    # Lives under natives/<id>/ so uninstall can remove the whole dir, and so a
    # mod shipping sibling data (ini, lang/) keeps it next to the dll.
    assert (me3 / "natives" / "questpath") in Path(dll).parents
    assert (me3 / "natives" / "questpath" / "QuestPath" / "QuestPath.ini").exists()


def test_install_me3_native_is_idempotent(tmp_path):
    src = _zip(tmp_path / "qp.zip", **{"QuestPath|QuestPath.dll": b"MZ"})
    me3 = tmp_path / "tools" / "me3"
    first = install_me3_native(src, "questpath", me3)
    second = install_me3_native(src, "questpath", me3)
    assert first == second
    assert Path(second).exists()


def test_install_me3_native_rejects_an_archive_with_no_dll(tmp_path):
    src = _zip(tmp_path / "nodll.zip", **{"readme.txt": b"hi"})
    with pytest.raises(PathError) as exc:
        install_me3_native(src, "questpath", tmp_path / "tools" / "me3")
    assert "questpath" in str(exc.value)


def test_install_me3_native_names_the_candidates_when_ambiguous(tmp_path):
    src = _zip(tmp_path / "two.zip", **{"alpha.dll": b"MZ", "beta.dll": b"MZ"})
    with pytest.raises(PathError) as exc:
        install_me3_native(src, "questpath", tmp_path / "tools" / "me3")
    # The message has to name them, or there's no way to pick a `dll` value.
    assert "alpha.dll" in str(exc.value) and "beta.dll" in str(exc.value)


def test_install_me3_native_honours_an_explicit_dll_choice(tmp_path):
    src = _zip(tmp_path / "two.zip", **{"alpha.dll": b"MZ", "beta.dll": b"MZ"})
    me3 = tmp_path / "tools" / "me3"
    dll = install_me3_native(src, "questpath", me3, dll="beta.dll")
    assert Path(dll).name == "beta.dll"


def test_install_me3_native_refuses_an_unsafe_dll_path(tmp_path):
    src = _zip(tmp_path / "two.zip", **{"alpha.dll": b"MZ"})
    with pytest.raises(PathError):
        install_me3_native(src, "questpath", tmp_path / "tools" / "me3",
                           dll="../../escape.dll")


# --- state ---

def test_record_and_list_me3_natives():
    s = {}
    state_mod.record_me3_native(s, "questpath", "1.3.0", "qp.zip",
                                "tools/me3/natives/questpath/QuestPath/QuestPath.dll")
    assert s["questpath"]["kind"] == "me3-native"
    assert state_mod.me3_natives(s) == [
        ("questpath", "tools/me3/natives/questpath/QuestPath/QuestPath.dll")]


def test_me3_natives_are_sorted_and_ignore_other_kinds():
    s = {}
    state_mod.record_me3_native(s, "zebra", "1", "z.zip", "tools/me3/natives/zebra/z.dll")
    state_mod.record_me3_native(s, "alpha", "1", "a.zip", "tools/me3/natives/alpha/a.dll")
    state_mod.record_me3_package(s, "pkg", "1", "p.zip", "tools/me3/mods/pkg")
    state_mod.record_install(s, "plain", "1", "g.zip", ["mods/x.dll"])
    assert [mid for mid, _ in state_mod.me3_natives(s)] == ["alpha", "zebra"]


def test_me3_natives_does_not_count_as_a_package():
    s = {}
    state_mod.record_me3_native(s, "questpath", "1", "q.zip", "tools/me3/natives/q/q.dll")
    # has_me3_packages drives the launch-method annotation; a native is
    # chainloaded, not a VFS asset override, so it must not flip that.
    assert state_mod.has_me3_packages(s) is False
    assert state_mod.me3_packages(s) == []


# --- profile generation ---

def test_reconcile_emits_a_native_for_each_recorded_dll(tmp_path):
    s = {}
    state_mod.record_me3_native(s, "questpath", "1", "q.zip",
                                str(tmp_path / "tools/me3/natives/questpath/QuestPath.dll"))
    text = reconcile(s, tmp_path / "tools" / "me3", _game(tmp_path)).read_text()
    assert text.count("[[natives]]") == 1
    assert "QuestPath.dll" in text


def test_reconcile_emits_natives_alongside_the_seamless_chainload(tmp_path):
    s = {"seamless-coop": {"files": []}}
    state_mod.record_me3_native(s, "questpath", "1", "q.zip",
                                str(tmp_path / "tools/me3/natives/questpath/QuestPath.dll"))
    text = reconcile(s, tmp_path / "tools" / "me3", _game(tmp_path)).read_text()
    assert text.count("[[natives]]") == 2
    assert "ersc.dll" in text and "QuestPath.dll" in text


def test_reconcile_native_paths_are_absolute(tmp_path):
    # me3 resolves a relative native against the profile dir; these live under
    # tools/me3/natives/, and the profile is regenerated from whatever cwd erm
    # ran in, so emit absolute and stop depending on it.
    s = {}
    state_mod.record_me3_native(s, "questpath", "1", "q.zip",
                                str(tmp_path / "tools/me3/natives/questpath/QuestPath.dll"))
    text = reconcile(s, tmp_path / "tools" / "me3", _game(tmp_path)).read_text()
    line = next(l for l in text.splitlines() if "QuestPath.dll" in l)
    assert "path = '/" in line or 'path = "/' in line


def test_reconcile_natives_sorted_by_id(tmp_path):
    s = {}
    state_mod.record_me3_native(s, "zebra", "1", "z.zip", str(tmp_path / "z" / "zebra.dll"))
    state_mod.record_me3_native(s, "alpha", "1", "a.zip", str(tmp_path / "a" / "alpha.dll"))
    text = reconcile(s, tmp_path / "tools" / "me3", _game(tmp_path)).read_text()
    assert text.index("alpha.dll") < text.index("zebra.dll")
