import pytest

from ermlib import conflicts


def _package(me3_dir, mod_id, files):
    for rel, content in files.items():
        p = me3_dir / "mods" / mod_id / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def test_index_lists_every_mod_claiming_a_path(tmp_path):
    _package(tmp_path, "a", {"msg/engus/x.dcx": b"1", "chr/only_a.dcx": b"2"})
    _package(tmp_path, "b", {"msg/engus/x.dcx": b"3"})
    index = conflicts.index_paths(tmp_path, ["a", "b"])
    assert index["msg/engus/x.dcx"] == ["a", "b"]
    assert index["chr/only_a.dcx"] == ["a"]


def test_index_ignores_mods_not_in_the_profile(tmp_path):
    _package(tmp_path, "a", {"msg/x.dcx": b"1"})
    _package(tmp_path, "stale", {"msg/x.dcx": b"2"})
    assert conflicts.index_paths(tmp_path, ["a"])["msg/x.dcx"] == ["a"]


def test_index_tolerates_a_listed_mod_that_was_never_installed(tmp_path):
    """A profile can list a mod id whose package was never extracted (fetch
    failed, install skipped). index_paths must not crash on it."""
    _package(tmp_path, "a", {"msg/x.dcx": b"1"})
    assert conflicts.index_paths(tmp_path, ["a", "never-installed"]) == {"msg/x.dcx": ["a"]}


def test_undeclared_collision_raises(tmp_path):
    """The whole point. A silently dropped mod is a correctness bug, so an
    unresolvable collision must stop the run rather than warn."""
    _package(tmp_path, "a", {"chr/c0000.anibnd.dcx": b"1"})
    _package(tmp_path, "b", {"chr/c0000.anibnd.dcx": b"2"})
    with pytest.raises(conflicts.ConflictError) as exc:
        conflicts.resolve(tmp_path, ["a", "b"], merges=[])
    assert "chr/c0000.anibnd.dcx" in str(exc.value)
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_no_collision_is_a_no_op(tmp_path):
    _package(tmp_path, "a", {"msg/x.dcx": b"1"})
    _package(tmp_path, "b", {"chr/y.dcx": b"2"})
    assert conflicts.resolve(tmp_path, ["a", "b"], merges=[]) == []
    assert not (tmp_path / "mods" / conflicts.MERGED_ID).exists()


def test_declared_merge_writes_to_the_merged_package(tmp_path):
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    spec = [{"path": "msg/x.dcx", "strategy": "concat-test",
             "mods": ["a", "b"], "prefer": "a"}]
    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    try:
        merged = conflicts.resolve(tmp_path, ["a", "b"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
    assert merged == ["msg/x.dcx"]
    assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/x.dcx").read_bytes() == b"AAABBB"


def test_merged_path_is_removed_from_its_sources(tmp_path):
    """The merged package must be the sole provider, so me3's load order can't
    decide the winner behind our back."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    spec = [{"path": "msg/x.dcx", "strategy": "concat-test",
             "mods": ["a", "b"], "prefer": "a"}]
    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    try:
        conflicts.resolve(tmp_path, ["a", "b"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
    assert not (tmp_path / "mods" / "a" / "msg/x.dcx").exists()
    assert not (tmp_path / "mods" / "b" / "msg/x.dcx").exists()


def test_merge_naming_an_unknown_strategy_raises(tmp_path):
    _package(tmp_path, "a", {"msg/x.dcx": b"A"})
    _package(tmp_path, "b", {"msg/x.dcx": b"B"})
    spec = [{"path": "msg/x.dcx", "strategy": "nope", "mods": ["a", "b"], "prefer": "a"}]
    with pytest.raises(conflicts.ConflictError):
        conflicts.resolve(tmp_path, ["a", "b"], merges=spec)


def test_merge_is_skipped_when_only_one_side_is_installed(tmp_path):
    """Profiles compose, so a merge can be inherited into a stack holding only
    one of its mods. That isn't an error and isn't a merge."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    spec = [{"path": "msg/x.dcx", "strategy": "fmg-union",
             "mods": ["a", "b"], "prefer": "a"}]
    assert conflicts.resolve(tmp_path, ["a"], merges=spec) == []
    assert (tmp_path / "mods" / "a" / "msg/x.dcx").exists()


def test_prune_removes_declared_paths(tmp_path):
    _package(tmp_path, "b", {"msg/dead.dcx": b"1", "msg/live.dcx": b"2"})
    pruned = conflicts.apply_prunes(tmp_path, [{"mod": "b", "paths": ["msg/dead.dcx"]}])
    assert pruned == ["b:msg/dead.dcx"]
    assert not (tmp_path / "mods" / "b" / "msg/dead.dcx").exists()
    assert (tmp_path / "mods" / "b" / "msg/live.dcx").exists()


def test_prune_of_a_missing_path_is_quiet(tmp_path):
    """A mod may stop shipping a dead file in a later version. That's the
    outcome the prune wanted, not a failure."""
    _package(tmp_path, "b", {"msg/live.dcx": b"2"})
    assert conflicts.apply_prunes(tmp_path, [{"mod": "b", "paths": ["msg/gone.dcx"]}]) == []


def test_prune_with_a_traversal_path_raises_instead_of_deleting(tmp_path):
    """A prune path comes straight out of a profile TOML, not the filesystem --
    unlike an index_paths result, nothing has confirmed it stays under the
    package dir. A `../` typo must not be able to delete a file elsewhere."""
    _package(tmp_path, "b", {"msg/live.dcx": b"2"})
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not delete me")
    with pytest.raises(conflicts.ConflictError):
        conflicts.apply_prunes(tmp_path, [{"mod": "b", "paths": ["../outside.txt"]}])
    assert outside.exists()


def test_merge_naming_a_prefer_not_among_providers_raises(tmp_path):
    """A stale or typo'd `prefer` must fail with a clear message, not a raw
    FileNotFoundError from trying to read a mod that never provided this path."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    spec = [{"path": "msg/x.dcx", "strategy": "fmg-union",
             "mods": ["a", "b"], "prefer": "c"}]
    with pytest.raises(conflicts.ConflictError) as exc:
        conflicts.resolve(tmp_path, ["a", "b"], merges=spec)
    assert "c" in str(exc.value)
