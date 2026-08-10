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


def test_rename_moves_a_declared_path(tmp_path):
    """Mod authors disagree about the case of the localisation directory. me3
    mounts one file per path, so a mod shipping `msg/engUS/x` claims a different
    slot from one shipping `msg/engus/x` and neither merges with the other."""
    _package(tmp_path, "b", {"msg/engUS/item.dcx": b"text"})
    moved = conflicts.apply_renames(
        tmp_path, [{"mod": "b", "paths": {"msg/engUS/item.dcx": "msg/engus/item.dcx"}}])
    assert moved == ["b:msg/engUS/item.dcx -> msg/engus/item.dcx"]
    assert (tmp_path / "mods" / "b" / "msg/engus/item.dcx").read_bytes() == b"text"
    assert not (tmp_path / "mods" / "b" / "msg/engUS/item.dcx").exists()


def test_rename_of_a_missing_source_is_quiet(tmp_path):
    """Same reasoning as a prune of a missing file: the mod may have fixed its
    own packaging upstream, which is the outcome the rename was asking for."""
    _package(tmp_path, "b", {"msg/engus/item.dcx": b"text"})
    assert conflicts.apply_renames(
        tmp_path, [{"mod": "b", "paths": {"msg/engUS/item.dcx": "msg/engus/item.dcx"}}]) == []
    assert (tmp_path / "mods" / "b" / "msg/engus/item.dcx").read_bytes() == b"text"


def test_rename_onto_a_file_the_mod_already_ships_raises(tmp_path):
    """If the mod ships both cases, moving one over the other destroys content
    without anyone asking. Refuse and make the author say which one wins."""
    _package(tmp_path, "b", {"msg/engUS/item.dcx": b"upper", "msg/engus/item.dcx": b"lower"})
    with pytest.raises(conflicts.ConflictError):
        conflicts.apply_renames(
            tmp_path, [{"mod": "b", "paths": {"msg/engUS/item.dcx": "msg/engus/item.dcx"}}])
    assert (tmp_path / "mods" / "b" / "msg/engus/item.dcx").read_bytes() == b"lower"


def test_rename_with_a_traversal_path_raises_instead_of_moving(tmp_path):
    """Both ends come out of a profile TOML, so both need the guard — a `../`
    destination would write outside the package, a `../` source read from it."""
    _package(tmp_path, "b", {"msg/live.dcx": b"2"})
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not touch me")
    with pytest.raises(conflicts.ConflictError):
        conflicts.apply_renames(
            tmp_path, [{"mod": "b", "paths": {"msg/live.dcx": "../escaped.dcx"}}])
    with pytest.raises(conflicts.ConflictError):
        conflicts.apply_renames(
            tmp_path, [{"mod": "b", "paths": {"../outside.txt": "msg/stolen.dcx"}}])
    assert outside.read_bytes() == b"do not touch me"
    assert not (tmp_path / "escaped.dcx").exists()


def test_rename_lets_a_case_variant_path_join_a_merge(tmp_path):
    """The point of the whole facility: two mods that both edit the localisation
    file are refused outright while their paths differ only in case, because
    nothing can tell which one me3 would mount. Normalised, they merge."""
    _package(tmp_path, "a", {"msg/engus/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/engUS/x.dcx": b"BBB"})
    spec = [{"path": "msg/engus/x.dcx", "strategy": "fmg-union",
             "mods": ["a", "b"], "prefer": "a"}]
    with pytest.raises(conflicts.ConflictError):
        conflicts.resolve(tmp_path, ["a", "b"], merges=spec)

    conflicts.apply_renames(
        tmp_path, [{"mod": "b", "paths": {"msg/engUS/x.dcx": "msg/engus/x.dcx"}}])
    index = conflicts.index_paths(tmp_path, ["a", "b"])
    assert sorted(index["msg/engus/x.dcx"]) == ["a", "b"]


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


def test_resolve_is_atomic_on_a_later_path_failure(tmp_path):
    """If the second path's strategy raises, the first path's already-computed
    merge must not have been committed -- otherwise a bare re-run has nothing
    to recover the first path's already-migrated sources from."""
    _package(tmp_path, "a", {"msg/a.dcx": b"A1", "msg/b.dcx": b"B1"})
    _package(tmp_path, "c", {"msg/a.dcx": b"A2", "msg/b.dcx": b"B2"})

    def boom(base, other):
        raise RuntimeError("strategy blew up")

    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    conflicts.STRATEGIES["boom-test"] = boom
    spec = [
        {"path": "msg/a.dcx", "strategy": "concat-test", "mods": ["a", "c"], "prefer": "a"},
        {"path": "msg/b.dcx", "strategy": "boom-test", "mods": ["a", "c"], "prefer": "a"},
    ]
    try:
        with pytest.raises(RuntimeError):
            conflicts.resolve(tmp_path, ["a", "c"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
        del conflicts.STRATEGIES["boom-test"]

    assert (tmp_path / "mods" / "a" / "msg/a.dcx").exists()
    assert (tmp_path / "mods" / "c" / "msg/a.dcx").exists()
    assert not (tmp_path / "mods" / conflicts.MERGED_ID).exists()


def test_clear_merged_after_a_failed_resolve_allows_a_clean_retry(tmp_path):
    """Covers clear_merged, and specifically the interaction with a failed
    resolve(): after a strategy blows up mid-run, clear_merged() must be safe
    to call even though nothing was committed, and a corrected re-run must
    succeed from the still-intact sources rather than a half-migrated tree."""
    _package(tmp_path, "a", {"msg/a.dcx": b"A1", "msg/b.dcx": b"B1"})
    _package(tmp_path, "c", {"msg/a.dcx": b"A2", "msg/b.dcx": b"B2"})

    def boom(base, other):
        raise RuntimeError("strategy blew up")

    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    conflicts.STRATEGIES["boom-test"] = boom
    bad_spec = [
        {"path": "msg/a.dcx", "strategy": "concat-test", "mods": ["a", "c"], "prefer": "a"},
        {"path": "msg/b.dcx", "strategy": "boom-test", "mods": ["a", "c"], "prefer": "a"},
    ]
    try:
        with pytest.raises(RuntimeError):
            conflicts.resolve(tmp_path, ["a", "c"], merges=bad_spec)

        conflicts.clear_merged(tmp_path)  # must not raise; nothing was ever written

        good_spec = [
            {"path": "msg/a.dcx", "strategy": "concat-test", "mods": ["a", "c"], "prefer": "a"},
            {"path": "msg/b.dcx", "strategy": "concat-test", "mods": ["a", "c"], "prefer": "a"},
        ]
        merged = conflicts.resolve(tmp_path, ["a", "c"], merges=good_spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
        del conflicts.STRATEGIES["boom-test"]

    assert set(merged) == {"msg/a.dcx", "msg/b.dcx"}
    assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/a.dcx").read_bytes() == b"A1A2"
    assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/b.dcx").read_bytes() == b"B1B2"


def test_merge_raises_when_an_undeclared_mod_also_provides_the_path(tmp_path):
    """A merge declared for mods=[a, b] must not silently fold in content from
    an unrelated mod c that happens to ship the same path -- that's unreviewed
    content silently included, the same class of bug as content dropped."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    _package(tmp_path, "c", {"msg/x.dcx": b"CCC"})
    spec = [{"path": "msg/x.dcx", "strategy": "concat-test", "mods": ["a", "b"], "prefer": "a"}]
    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    try:
        with pytest.raises(conflicts.ConflictError) as exc:
            conflicts.resolve(tmp_path, ["a", "b", "c"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
    msg = str(exc.value)
    assert "msg/x.dcx" in msg
    assert "c" in msg


def test_merge_proceeds_when_a_declared_mod_is_simply_not_installed(tmp_path):
    """Profiles compose: a merge inherited into a stack can name a mod that
    stack never installs. As long as every actual provider is among the
    declared mods, that's still the collision the merge was written for --
    just missing an optional participant, not an unreviewed stranger."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    spec = [{"path": "msg/x.dcx", "strategy": "concat-test",
             "mods": ["a", "b", "c"], "prefer": "a"}]
    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    try:
        merged = conflicts.resolve(tmp_path, ["a", "b"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
    assert merged == ["msg/x.dcx"]
    assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/x.dcx").read_bytes() == b"AAABBB"


def test_case_only_path_collision_is_detected(tmp_path):
    """me3's runtime case-sensitivity is unverified, so we don't normalize or
    guess -- two paths that collide only under case-folding must raise and
    name the ambiguity, since index_paths would otherwise file them as two
    unrelated one-provider entries and the collision would never surface."""
    _package(tmp_path, "a", {"msg/engus/x.dcx": b"1"})
    _package(tmp_path, "b", {"MSG/ENGUS/X.dcx": b"2"})
    with pytest.raises(conflicts.ConflictError) as exc:
        conflicts.resolve(tmp_path, ["a", "b"], merges=[])
    msg = str(exc.value)
    assert "msg/engus/x.dcx" in msg
    assert "MSG/ENGUS/X.dcx" in msg


def test_conflicting_merge_declarations_for_the_same_path_raise(tmp_path):
    """Two composed profiles can each declare a different merge for the same
    path -- manifest.py's dedup key is (path, mods), so entries that differ in
    `prefer` (or strategy, or mods) both survive and reach resolve(). Plain
    dict-overwrite would silently keep one and drop the other's declaration
    (and here would go on to actually complete a merge using it); it must
    raise instead and name the path."""
    _package(tmp_path, "a", {"msg/x.dcx": b"AAA"})
    _package(tmp_path, "b", {"msg/x.dcx": b"BBB"})
    spec = [
        {"path": "msg/x.dcx", "strategy": "concat-test", "mods": ["a", "b"], "prefer": "a"},
        {"path": "msg/x.dcx", "strategy": "concat-test", "mods": ["a", "b"], "prefer": "b"},
    ]
    conflicts.STRATEGIES["concat-test"] = lambda base, other: base + other
    try:
        with pytest.raises(conflicts.ConflictError) as exc:
            conflicts.resolve(tmp_path, ["a", "b"], merges=spec)
    finally:
        del conflicts.STRATEGIES["concat-test"]
    assert "msg/x.dcx" in str(exc.value)


# --- vanilla sourcing for three-way strategies ---

def _vanilla_zip(tmp_path, member, content, asset="van.zip"):
    import zipfile
    vendor = tmp_path / "vendor"
    vendor.mkdir(exist_ok=True)
    with zipfile.ZipFile(vendor / asset, "w") as z:
        z.writestr(member, content)
    return {"randomizer": {"asset": asset}}


def _three_way_spec(member, strategy="fake-3way"):
    return [{"path": "msg/x.dcx", "mods": ["a", "b"], "prefer": "a",
             "strategy": strategy,
             "vanilla": {"mod": "randomizer", "member": member}}]


@pytest.fixture
def fake_three_way(monkeypatch):
    """A strategy that records the vanilla it was handed, so the plumbing can be
    tested without dragging a real DCX/BND4 through it."""
    seen = {}

    def strategy(base, other, vanilla):
        seen["args"] = (base, other, vanilla)
        return b"merged"

    monkeypatch.setitem(conflicts.STRATEGIES, "fake-3way", strategy)
    monkeypatch.setattr(conflicts, "NEEDS_VANILLA",
                        conflicts.NEEDS_VANILLA | {"fake-3way"})
    return seen


def test_a_three_way_strategy_receives_the_declared_vanilla(tmp_path, monkeypatch, fake_three_way):
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "diste/Vanilla/msg/x.dcx", b"vanilla bytes")
    conflicts.resolve(tmp_path, ["a", "b"],
                      _three_way_spec("diste/Vanilla/msg/x.dcx"), lock=lock)
    assert fake_three_way["args"] == (b"base", b"other", b"vanilla bytes")


def test_a_three_way_merge_without_a_vanilla_declaration_raises(tmp_path, monkeypatch, fake_three_way):
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    spec = _three_way_spec("unused")
    del spec[0]["vanilla"]
    with pytest.raises(conflicts.ConflictError, match="vanilla"):
        conflicts.resolve(tmp_path, ["a", "b"], spec, lock={})


def test_a_missing_vanilla_member_names_the_archive(tmp_path, monkeypatch, fake_three_way):
    """A typo'd member would otherwise surface as a bare KeyError from zipfile,
    pointing at nothing the profile author can act on."""
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "diste/Vanilla/msg/x.dcx", b"v")
    with pytest.raises(conflicts.ConflictError, match="not in"):
        conflicts.resolve(tmp_path, ["a", "b"],
                          _three_way_spec("diste/Vanilla/typo.dcx"), lock=lock)


def test_a_vanilla_mod_missing_from_the_lock_raises(tmp_path, monkeypatch, fake_three_way):
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    with pytest.raises(conflicts.ConflictError, match="randomizer"):
        conflicts.resolve(tmp_path, ["a", "b"],
                          _three_way_spec("diste/Vanilla/msg/x.dcx"), lock={})


def test_an_unsafe_vanilla_member_is_refused(tmp_path, monkeypatch, fake_three_way):
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "diste/Vanilla/msg/x.dcx", b"v")
    with pytest.raises(conflicts.ConflictError, match="unsafe"):
        conflicts.resolve(tmp_path, ["a", "b"],
                          _three_way_spec("../../etc/passwd"), lock=lock)


@pytest.fixture
def fake_notes_strategy(monkeypatch):
    """A NEEDS_VANILLA strategy that also reports back things it couldn't
    carry over, the way esd-3way does. Registered under NEEDS_NOTES too, so
    resolve() has to hand it somewhere to put them."""
    def strategy(base, other, vanilla, notes=None):
        if notes is not None:
            notes.append(f"lost something merging past {vanilla!r}")
        return base + other

    monkeypatch.setitem(conflicts.STRATEGIES, "fake-notes", strategy)
    monkeypatch.setattr(conflicts, "NEEDS_VANILLA", conflicts.NEEDS_VANILLA | {"fake-notes"})
    monkeypatch.setattr(conflicts, "NEEDS_NOTES", conflicts.NEEDS_NOTES | {"fake-notes"})


def test_a_strategy_needing_notes_gets_its_note_back_tagged_with_the_path(
        tmp_path, monkeypatch, fake_notes_strategy):
    """A profile can merge more than one path this way, so a bare note isn't
    enough -- the caller (ultimately the apply report) needs to know which
    file it's about."""
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "V/x.dcx", b"vanilla")
    notes = []
    conflicts.resolve(tmp_path, ["a", "b"],
                      _three_way_spec("V/x.dcx", strategy="fake-notes"), lock=lock, notes=notes)
    assert notes == [("msg/x.dcx", "lost something merging past b'vanilla'")]


def test_resolve_without_a_notes_list_does_not_raise(tmp_path, monkeypatch, fake_notes_strategy):
    """Most callers of resolve() (and every existing test) don't pass notes= at
    all -- a strategy that wants to report something must not require one."""
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "V/x.dcx", b"vanilla")
    merged = conflicts.resolve(tmp_path, ["a", "b"],
                               _three_way_spec("V/x.dcx", strategy="fake-notes"), lock=lock)
    assert merged == ["msg/x.dcx"]


def test_a_strategy_not_needing_notes_is_still_called_with_two_or_three_arguments(
        tmp_path, monkeypatch, fake_three_way):
    """NEEDS_NOTES is opt-in per strategy: fake_three_way's strategy only takes
    (base, other, vanilla), and resolve() must not start passing it a fourth
    positional/keyword argument just because some other strategy wants one."""
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"base"})
    _package(tmp_path, "b", {"msg/x.dcx": b"other"})
    lock = _vanilla_zip(tmp_path, "V/x.dcx", b"vanilla")
    notes = []
    conflicts.resolve(tmp_path, ["a", "b"], _three_way_spec("V/x.dcx"), lock=lock, notes=notes)
    assert notes == []


def test_a_two_way_strategy_in_needs_notes_still_gets_a_notes_list(tmp_path, monkeypatch):
    """NEEDS_NOTES and NEEDS_VANILLA are independent: a strategy can want notes
    without wanting vanilla. Branching on "has vanilla" before ever looking at
    NEEDS_NOTES would strand this combination -- a note appended to a `None`
    the strategy was silently handed instead of the list the caller is reading
    from, with no error anywhere to say so. tpf-union is the closest thing to
    a live candidate for this today: it silently keeps base's copy of a shared
    texture, with nowhere to say so once it's registered under NEEDS_NOTES."""
    def strategy(base, other, notes=None):
        if notes is not None:
            notes.append("a two-way strategy's own note")
        return base + other

    monkeypatch.setitem(conflicts.STRATEGIES, "fake-2way-notes", strategy)
    monkeypatch.setattr(conflicts, "NEEDS_NOTES", conflicts.NEEDS_NOTES | {"fake-2way-notes"})

    _package(tmp_path, "a", {"msg/x.dcx": b"A"})
    _package(tmp_path, "b", {"msg/x.dcx": b"B"})
    spec = [{"path": "msg/x.dcx", "mods": ["a", "b"], "prefer": "a",
             "strategy": "fake-2way-notes"}]
    notes = []
    conflicts.resolve(tmp_path, ["a", "b"], spec, notes=notes)
    assert notes == [("msg/x.dcx", "a two-way strategy's own note")]


def test_a_two_way_strategy_still_takes_two_arguments(tmp_path):
    """fmg-union predates vanilla sourcing and must keep working untouched."""
    _package(tmp_path, "a", {"msg/x.dcx": b"1"})
    _package(tmp_path, "b", {"msg/x.dcx": b"2"})
    spec = [{"path": "msg/x.dcx", "mods": ["a", "b"], "prefer": "a",
             "strategy": "concat"}]
    conflicts.STRATEGIES["concat"] = lambda base, other: base + other
    try:
        conflicts.resolve(tmp_path, ["a", "b"], spec)
        assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/x.dcx").read_bytes() == b"12"
    finally:
        del conflicts.STRATEGIES["concat"]


def test_resolve_folds_a_merge_across_three_providers(tmp_path):
    """Three mods claiming one path is one merge folded twice, not a special
    case. Every contributor must also lose the path afterwards -- leaving one
    behind would put the file back into collision on the next apply."""
    _package(tmp_path, "a", {"msg/x.dcx": b"A"})
    _package(tmp_path, "b", {"msg/x.dcx": b"B"})
    _package(tmp_path, "c", {"msg/x.dcx": b"C"})
    spec = [{"path": "msg/x.dcx", "mods": ["a", "b", "c"], "prefer": "a",
             "strategy": "concat"}]
    conflicts.STRATEGIES["concat"] = lambda base, other: base + other
    try:
        assert conflicts.resolve(tmp_path, ["a", "b", "c"], spec) == ["msg/x.dcx"]
    finally:
        del conflicts.STRATEGIES["concat"]
    # prefer first, then the rest -- the fold order the strategies assume
    assert (tmp_path / "mods" / conflicts.MERGED_ID / "msg/x.dcx").read_bytes() == b"ABC"
    for mod_id in ("a", "b", "c"):
        assert not (tmp_path / "mods" / mod_id / "msg/x.dcx").exists()


def test_a_three_way_strategy_gets_vanilla_on_every_fold_step(tmp_path, monkeypatch, fake_three_way):
    """Vanilla is the fixed reference for all N-1 merges. Passing it only on the
    first step would make every later contributor compare against the running
    result instead, quietly turning a three-way merge into a two-way one."""
    monkeypatch.chdir(tmp_path)
    _package(tmp_path, "a", {"msg/x.dcx": b"A"})
    _package(tmp_path, "b", {"msg/x.dcx": b"B"})
    _package(tmp_path, "c", {"msg/x.dcx": b"C"})
    lock = _vanilla_zip(tmp_path, "V/x.dcx", b"VAN")
    seen = []
    monkeypatch.setitem(conflicts.STRATEGIES, "fake-3way",
                        lambda base, other, vanilla: seen.append((base, other, vanilla)) or base + other)
    spec = [{"path": "msg/x.dcx", "mods": ["a", "b", "c"], "prefer": "a",
             "strategy": "fake-3way", "vanilla": {"mod": "randomizer", "member": "V/x.dcx"}}]
    conflicts.resolve(tmp_path, ["a", "b", "c"], spec, lock=lock)
    assert [s[2] for s in seen] == [b"VAN", b"VAN"]
    assert [s[1] for s in seen] == [b"B", b"C"]
    assert seen[1][0] == b"AB"          # the running result feeds the next step


# --- the real esd-3way strategy, driven through resolve() itself -----------


def test_resolve_drives_the_real_esd_three_way_strategy_and_returns_its_note(
        tmp_path, monkeypatch):
    """Every other test that reaches resolve() with a NEEDS_VANILLA/NEEDS_NOTES
    strategy uses a fake whose signature the test itself wrote -- so renaming
    esd_three_way's `notes` parameter would leave the whole suite green while a
    real apply died with TypeError on the one hop this module exists to cover.
    This drives the actual "esd-3way" strategy name, registered the normal
    way, against the real Boss Res / Melina / vanilla .talkesdbnd.dcx blobs."""
    from ermlib import esdmerge
    from ermlib.formats import bnd4, dcx, esd
    from tests.esd_fixtures import talkesd_container

    # Read the real archives (relative to the repo's own vendor/) before
    # chdir'ing into tmp_path -- talkesd_container looks under "vendor/"
    # relative to the CWD, and _vanilla_zip/resolve() need that to be tmp_path.
    boss_container = talkesd_container("bossres")
    melina_container = talkesd_container("melina")
    vanilla_container = talkesd_container("vanilla")
    # These are Kraken-compressed, and ooz.py resolves its vendored source and
    # build cache (vendor-src/, tools/ooz/) relative to CWD too -- decompress
    # one now, while CWD is still the repo root, so the chdir below doesn't
    # break the first real build/load of libooz.so. Once loaded it's cached
    # in ooz._lib for the rest of the process regardless of CWD.
    dcx.read(boss_container)

    monkeypatch.chdir(tmp_path)
    path = "script/talk/m00_00_00_00.talkesdbnd.dcx"
    _package(tmp_path, "bossres", {path: boss_container})
    _package(tmp_path, "melina", {path: melina_container})
    lock = _vanilla_zip(tmp_path, "V/vanilla.talkesdbnd.dcx", vanilla_container, asset="van.zip")

    spec = [{"path": path, "mods": ["bossres", "melina"], "prefer": "bossres",
             "strategy": "esd-3way",
             "vanilla": {"mod": "randomizer", "member": "V/vanilla.talkesdbnd.dcx"}}]
    notes = []
    merged = conflicts.resolve(tmp_path, ["bossres", "melina"], spec, lock=lock, notes=notes)

    assert merged == [path]
    # Melina's hook on vanilla state 26 collides with Boss Res deleting it --
    # the one thing this real pair cannot compose cleanly (see esdmerge's own
    # test_the_real_shared_group_replays_melina_onto_boss_res).
    assert len(notes) == 1
    rel, note = notes[0]
    assert rel == path
    assert note.group == 2147483624
    assert note.state == 26
    assert note.reason is esdmerge.Reason.NOT_IN_BASE

    merged_bytes = (tmp_path / "mods" / conflicts.MERGED_ID / path).read_bytes()
    entries = bnd4.read(dcx.read(merged_bytes))
    assert len(entries) == 7
    for entry in entries:
        esdmerge.check(esd.read(entry.data))
