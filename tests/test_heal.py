import hashlib

import pytest

from ermlib import heal
from ermlib.gamebuild import BuildId, GameBuildError
from ermlib.heal import HealError
from tests.test_doctor import _regulation_blob


def _bid(**over):
    base = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
                steam_buildid="23850278", regulation_sha="a" * 64)
    base.update(over)
    return BuildId(**base)


def test_baseline_path_is_named_for_the_build(tmp_path):
    assert heal.baseline_path("11701000", tmp_path).name == "regulation-11701000.bin"


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def test_adopting_copies_the_games_regulation(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    blob = b"the 1.17 regulation"
    (game / "regulation.bin").write_bytes(blob)
    dest = heal.adopt_baseline(game, _bid(regulation_sha=_sha(blob)), tmp_path / "baselines")
    assert dest.read_bytes() == b"the 1.17 regulation"
    assert dest.name == "regulation-11701000.bin"


def test_adopting_is_idempotent_and_keeps_the_first_copy(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(b"first")
    base = tmp_path / "baselines"
    heal.adopt_baseline(game, _bid(regulation_sha=_sha(b"first")), base)
    (game / "regulation.bin").write_bytes(b"second")
    dest = heal.adopt_baseline(game, _bid(regulation_sha=_sha(b"second")), base)
    # Same build id -> same baseline. Re-copying would let a later tampered
    # install quietly replace a baseline we already trusted.
    assert dest.read_bytes() == b"first"


def test_old_baselines_are_kept_so_a_rebuild_stays_reproducible(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    base = tmp_path / "baselines"
    (game / "regulation.bin").write_bytes(b"1.16")
    heal.adopt_baseline(
        game, _bid(regulation="11601000", regulation_sha=_sha(b"1.16")), base)
    (game / "regulation.bin").write_bytes(b"1.17")
    heal.adopt_baseline(game, _bid(regulation_sha=_sha(b"1.17")), base)
    assert {p.name for p in base.iterdir()} == {
        "regulation-11601000.bin", "regulation-11701000.bin"}


def test_a_missing_game_regulation_raises(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    with pytest.raises(HealError, match="regulation.bin"):
        heal.adopt_baseline(game, _bid(), tmp_path / "baselines")


def test_adopting_refuses_when_the_file_changed_since_it_was_identified(tmp_path):
    # The identity was read from this file moments earlier. Different bytes now
    # mean something is writing to the install underneath the heal, and the blob
    # in hand is not the one that was judged safe to trust.
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(b"not what was identified")
    with pytest.raises(HealError, match="changed between planning and adopting"):
        heal.adopt_baseline(game, _bid(regulation_sha="a" * 64), tmp_path / "baselines")


def test_adopting_accepts_the_file_it_was_identified_from(tmp_path):
    import hashlib
    game = tmp_path / "Game"
    game.mkdir()
    blob = b"the 1.17 regulation"
    (game / "regulation.bin").write_bytes(blob)
    dest = heal.adopt_baseline(
        game, _bid(regulation_sha=hashlib.sha256(blob).hexdigest()),
        tmp_path / "baselines")
    assert dest.read_bytes() == blob


def test_an_already_adopted_baseline_is_not_rehashed(tmp_path):
    # The early return must come before any read of the game file, so a stale
    # or mismatched install cannot invalidate a baseline already trusted.
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(b"whatever")
    base = tmp_path / "baselines"
    base.mkdir()
    (base / "regulation-11701000.bin").write_bytes(b"already here")
    dest = heal.adopt_baseline(game, _bid(regulation_sha="0" * 64), base)
    assert dest.read_bytes() == b"already here"


def test_layout_gate_passes_when_every_stride_matches(monkeypatch):
    layouts = {"EquipParamWeapon.param": heal.Layout(200, 1024, 3),
               "SpEffectParam.param": heal.Layout(90, 512, 2)}
    monkeypatch.setattr(heal, "param_layouts", lambda blob: layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_layout_gate_names_the_table_and_mod_when_a_stride_moves(monkeypatch):
    def layouts(blob):
        if blob == b"base":
            return {"EquipParamWeapon.param": heal.Layout(200, 1032, 3)}
        return {"EquipParamWeapon.param": heal.Layout(200, 1024, 3)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("clevers", b"mod")])
    assert len(problems) == 1
    assert "clevers" in problems[0]
    assert "EquipParamWeapon.param" in problems[0]
    assert "1024" in problems[0] and "1032" in problems[0]


def test_layout_gate_catches_a_moved_paramdef_version(monkeypatch):
    def layouts(blob):
        return {"SpEffectParam.param": heal.Layout(90, 512, 4 if blob == b"base" else 2)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("nofalldead", b"mod")])
    assert len(problems) == 1
    assert "paramdef" in problems[0]


def test_a_table_the_baseline_lacks_is_not_a_layout_problem(monkeypatch):
    # A mod may ship a table the baseline doesn't carry. That is a merge
    # question, not a transplant-safety one -- the gate must not claim it.
    def layouts(blob):
        return {} if blob == b"base" else {"Odd.param": heal.Layout(4, 8, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("weird", b"mod")]) == ()


def test_layout_gate_reports_every_mod_not_just_the_first(monkeypatch):
    def layouts(blob):
        if blob == b"base":
            return {"A.param": heal.Layout(7, 10, 1)}
        return {"A.param": heal.Layout(7, 12, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("a", b"m1"), ("b", b"m2")])
    assert len(problems) == 2


def test_layout_gate_skips_a_table_the_baseline_cannot_read(monkeypatch):
    def layouts(blob):
        return ({"Cutscene.param": None} if blob == b"base"
                else {"Cutscene.param": heal.Layout(107, 16, 1)})
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_layout_gate_skips_a_table_the_mod_cannot_read(monkeypatch):
    def layouts(blob):
        return ({"Cutscene.param": heal.Layout(107, 16, 1)} if blob == b"base"
                else {"Cutscene.param": None})
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_param_layouts_records_an_unreadable_table_as_none(monkeypatch):
    class _Entry:
        name = "GR\\Cutscene.param"
        data = b"junk"
    monkeypatch.setattr(heal.regulation, "entries", lambda blob: [_Entry()])
    def _boom(data):
        raise heal.param.ParamError("strings offset past the end of the file")
    monkeypatch.setattr(heal.param, "read", _boom)
    assert heal.param_layouts(b"anything") == {"Cutscene.param": None}


def test_layout_gate_ignores_a_padding_only_stride_on_a_single_row_param(monkeypatch):
    # A one-row param has no inter-row gap, so its "stride" is the row plus
    # whatever alignment the writer left -- 16 in the game's own file, 8 in a
    # re-saved mod copy. Ten real params differ that way with nothing moved.
    def layouts(blob):
        if blob == b"base":
            return {"PlayerCommonParam.param": heal.Layout(1, 264, 1)}
        return {"PlayerCommonParam.param": heal.Layout(1, 256, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_layout_gate_still_compares_paramdef_version_on_a_single_row_param(monkeypatch):
    # The stride is unusable there, but paramdef_data_version still says
    # whether the fields inside the row moved -- so it is still checked.
    def layouts(blob):
        if blob == b"base":
            return {"PlayerCommonParam.param": heal.Layout(1, 264, 2)}
        return {"PlayerCommonParam.param": heal.Layout(1, 256, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("clevers", b"mod")])
    assert len(problems) == 1
    assert "paramdef" in problems[0]


def test_layout_gate_reports_a_stride_that_moved_on_a_multi_row_param(monkeypatch):
    def layouts(blob):
        if blob == b"base":
            return {"SpEffectParam.param": heal.Layout(2, 264, 1)}
        return {"SpEffectParam.param": heal.Layout(2, 256, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("clevers", b"mod")])
    assert len(problems) == 1
    assert "row stride" in problems[0]


def test_param_layouts_records_the_row_count(monkeypatch):
    from tests.test_param import make_param

    class _Entry:
        name = "GR\\PlayerCommonParam.param"
        data = make_param([1], stride=64)
    monkeypatch.setattr(heal.regulation, "entries", lambda blob: [_Entry()])
    got = heal.param_layouts(b"anything")["PlayerCommonParam.param"]
    assert got.rows == 1
    assert got.stride == 64


def test_rows_by_table_skips_a_table_it_cannot_read(monkeypatch):
    # merge.param_rows never row-splices an unreadable entry -- it takes the
    # whole entry from one side or raises MergeError -- so there are no
    # transplanted rows in these tables to verify. Omitting them beats
    # crashing on the three real regulation.bin tables FromSoft itself wrote
    # with a strings offset past the end of the file.
    class _Entry:
        name = "GR\\Cutscene.param"
        data = b"junk"
    monkeypatch.setattr(heal.regulation, "entries", lambda blob: [_Entry()])
    def _boom(data):
        raise heal.param.ParamError("strings offset past the end of the file")
    monkeypatch.setattr(heal.param, "read", _boom)
    assert heal.rows_by_table(b"anything") == {}


def test_verify_passes_when_every_authored_row_survived(monkeypatch):
    base = {"A.param": {1: b"v", 2: b"v"}}
    mod = {"A.param": {1: b"MOD", 2: b"v"}}
    merged = {"A.param": {1: b"MOD", 2: b"v", 3: b"new"}}
    blobs = {b"base": base, b"mod": mod, b"merged": merged}
    monkeypatch.setattr(heal, "rows_by_table", lambda b: blobs[b])
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11701000")
    assert heal.verify_rebase(b"merged", b"base", [("m", b"mod")], _bid()) == ()


def test_verify_catches_an_authored_row_lost_in_the_rebase(monkeypatch):
    base = {"A.param": {1: b"v"}}
    mod = {"A.param": {1: b"MOD"}}
    merged = {"A.param": {1: b"v"}}          # mod's row silently reverted
    blobs = {b"base": base, b"mod": mod, b"merged": merged}
    monkeypatch.setattr(heal, "rows_by_table", lambda b: blobs[b])
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11701000")
    problems = heal.verify_rebase(b"merged", b"base", [("m", b"mod")], _bid())
    assert len(problems) == 1
    assert "A.param" in problems[0] and "row 1" in problems[0]


def test_verify_ignores_rows_a_mod_did_not_author(monkeypatch):
    base = {"A.param": {1: b"v"}}
    mod = {"A.param": {1: b"v"}}             # identical to vanilla
    merged = {"A.param": {}}                 # dropped, but nobody authored it
    blobs = {b"base": base, b"mod": mod, b"merged": merged}
    monkeypatch.setattr(heal, "rows_by_table", lambda b: blobs[b])
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11701000")
    assert heal.verify_rebase(b"merged", b"base", [("m", b"mod")], _bid()) == ()


def test_verify_does_not_flag_a_row_two_mods_both_authored(monkeypatch):
    # Contested rows are resolved by `prefer` and reported by the merge itself.
    # Demanding both survive would fail every legitimate preferred merge.
    base = {"A.param": {1: b"v"}}
    merged = {"A.param": {1: b"ONE"}}
    blobs = {b"base": base, b"one": {"A.param": {1: b"ONE"}},
             b"two": {"A.param": {1: b"TWO"}}, b"merged": merged}
    monkeypatch.setattr(heal, "rows_by_table", lambda b: blobs[b])
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11701000")
    assert heal.verify_rebase(b"merged", b"base",
                              [("one", b"one"), ("two", b"two")], _bid()) == ()


def test_verify_requires_the_merged_output_to_claim_the_installed_build(monkeypatch):
    monkeypatch.setattr(heal, "rows_by_table", lambda b: {})
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11601000")
    problems = heal.verify_rebase(b"merged", b"base", [], _bid())
    assert any("11601000" in p and "11701000" in p for p in problems)


def test_verify_keeps_rows_the_new_baseline_added(monkeypatch):
    # 1.17 added 82 EquipParamWeapon rows. They come from the base and must
    # still be there after a rebase.
    base = {"A.param": {1: b"v", 99: b"new-in-1.17"}}
    mod = {"A.param": {1: b"MOD"}}
    merged = {"A.param": {1: b"MOD"}}        # base's new row went missing
    blobs = {b"base": base, b"mod": mod, b"merged": merged}
    monkeypatch.setattr(heal, "rows_by_table", lambda b: blobs[b])
    monkeypatch.setattr(heal, "read_regulation_version", lambda b: "11701000")
    problems = heal.verify_rebase(b"merged", b"base", [("m", b"mod")], _bid())
    assert any("99" in p for p in problems)


def _kinds(actions):
    return [a.kind for a in actions]


def test_no_drift_plans_nothing():
    assert heal.plan_heal(_bid(), _bid()) == ()


def test_an_unstamped_stack_plans_nothing():
    assert heal.plan_heal(None, _bid()) == ()


def test_a_patch_plans_the_full_rebase_in_order():
    stamped = _bid(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                   steam_buildid="1", regulation_sha="b" * 64)
    kinds = _kinds(heal.plan_heal(stamped, _bid()))
    assert kinds == ["adopt-baseline", "repin", "gate", "rebuild", "verify", "stamp"]


def test_the_gate_runs_after_fetching_not_before():
    # An updated mod may be the thing that fixes a layout mismatch, so the
    # question is whether THESE files merge onto THIS baseline.
    stamped = _bid(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                   steam_buildid="1", regulation_sha="b" * 64)
    kinds = _kinds(heal.plan_heal(stamped, _bid()))
    assert kinds.index("repin") < kinds.index("gate")


def test_a_repackaged_build_restamps_without_rebuilding():
    stamped = _bid(exe="2.6.2.0", steam_buildid="1")
    kinds = _kinds(heal.plan_heal(stamped, _bid()))
    assert "rebuild" not in kinds
    assert kinds[-1] == "stamp"


def test_a_tampered_install_refuses_and_plans_nothing_else():
    stamped = _bid(regulation_sha="b" * 64)
    actions = heal.plan_heal(stamped, _bid())
    assert _kinds(actions) == ["refuse"]
    assert "Verify integrity" in actions[0].detail


def test_a_stale_launcher_adds_a_reharden_step():
    stamped = _bid(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                   steam_buildid="1", regulation_sha="b" * 64)
    kinds = _kinds(heal.plan_heal(stamped, _bid(), launcher_stale=True))
    assert "reharden" in kinds
    assert kinds.index("reharden") < kinds.index("stamp")


def test_no_reharden_omits_it():
    stamped = _bid(exe="2.6.2.0", app="1.16.0", regulation="11601000",
                   steam_buildid="1", regulation_sha="b" * 64)
    kinds = _kinds(heal.plan_heal(stamped, _bid(), reharden=False, launcher_stale=True))
    assert "reharden" not in kinds


def _game_with_regulation(tmp_path, blob=b"the 1.17 regulation"):
    """A game dir holding `blob`, plus the identity that names those bytes."""
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(blob)
    return game, _bid(regulation_sha=_sha(blob))


def test_prepare_rebase_is_empty_when_the_ancestor_is_the_installed_build(tmp_path):
    # The mods branched from the build that is running, so there is nothing to
    # forward-port them onto.
    assert heal.prepare_rebase(
        tmp_path, _bid(), _regulation_blob("11701000"), [("clevers", b"mod")]) == {}


def test_prepare_rebase_is_empty_when_no_merge_declares_an_ancestor(tmp_path):
    assert heal.prepare_rebase(tmp_path, _bid(), None, [("clevers", b"mod")]) == {}


def test_prepare_rebase_is_empty_when_no_mod_ships_a_regulation(tmp_path):
    # An overlay profile whose packages carry no regulation.bin has nothing to
    # gate and nothing to fold, however far the game has moved.
    assert heal.prepare_rebase(
        tmp_path, _bid(), _regulation_blob("11611000"), []) == {}


def test_prepare_rebase_offers_the_adopted_baseline_for_an_older_ancestor(
        tmp_path, monkeypatch):
    game, live = _game_with_regulation(tmp_path)
    monkeypatch.setattr(heal, "layout_gate", lambda base, mods: ())
    out = heal.prepare_rebase(game, live, _regulation_blob("11611000"),
                              [("clevers", b"mod")], base=tmp_path / "baselines")
    assert out == {"regulation.bin": b"the 1.17 regulation"}


def test_prepare_rebase_answers_the_same_on_a_stack_stamped_to_the_game(
        tmp_path, monkeypatch):
    # The stamp says a previous apply already ran against this build. That says
    # nothing about what the merge would be built from, and keying the rebase
    # on it meant the second apply of a patched game quietly rebuilt the merge
    # against the old ancestor and reported success.
    game, live = _game_with_regulation(tmp_path)
    (tmp_path / "baselines").mkdir()
    (tmp_path / "baselines" / f"regulation-{live.regulation}.bin").write_bytes(
        b"the 1.17 regulation")
    monkeypatch.setattr(heal, "layout_gate", lambda base, mods: ())
    out = heal.prepare_rebase(game, live, _regulation_blob("11611000"),
                              [("clevers", b"mod")], base=tmp_path / "baselines")
    assert out == {"regulation.bin": b"the 1.17 regulation"}


def test_prepare_rebase_refuses_an_ancestor_it_cannot_read(tmp_path):
    # Silently skipping the rebase would leave the 1.16 merge mounted over a
    # 1.17 game, which is the state this whole path exists to prevent.
    with pytest.raises(GameBuildError):
        heal.prepare_rebase(tmp_path, _bid(), b"not a regulation",
                            [("clevers", b"mod")])


def test_prepare_rebase_stops_the_apply_when_a_layout_moved(tmp_path, monkeypatch):
    # A moved stride means rows cannot be transplanted. Warning and carrying on
    # would write a corrupted regulation.
    game, live = _game_with_regulation(tmp_path)
    monkeypatch.setattr(heal, "layout_gate",
                        lambda base, mods: ("clevers: X.param row stride 8 != baseline 16",))
    with pytest.raises(HealError, match="stride"):
        heal.prepare_rebase(game, live, _regulation_blob("11611000"),
                            [("clevers", b"mod")], base=tmp_path / "baselines")
