import pytest

from ermlib import heal
from ermlib.gamebuild import BuildId
from ermlib.heal import HealError


def _bid(**over):
    base = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
                steam_buildid="23850278", regulation_sha="a" * 64)
    base.update(over)
    return BuildId(**base)


def test_baseline_path_is_named_for_the_build(tmp_path):
    assert heal.baseline_path("11701000", tmp_path).name == "regulation-11701000.bin"


def test_adopting_copies_the_games_regulation(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(b"the 1.17 regulation")
    dest = heal.adopt_baseline(game, _bid(), tmp_path / "baselines")
    assert dest.read_bytes() == b"the 1.17 regulation"
    assert dest.name == "regulation-11701000.bin"


def test_adopting_is_idempotent_and_keeps_the_first_copy(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "regulation.bin").write_bytes(b"first")
    base = tmp_path / "baselines"
    heal.adopt_baseline(game, _bid(), base)
    (game / "regulation.bin").write_bytes(b"second")
    dest = heal.adopt_baseline(game, _bid(), base)
    # Same build id -> same baseline. Re-copying would let a later tampered
    # install quietly replace a baseline we already trusted.
    assert dest.read_bytes() == b"first"


def test_old_baselines_are_kept_so_a_rebuild_stays_reproducible(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    base = tmp_path / "baselines"
    (game / "regulation.bin").write_bytes(b"1.16")
    heal.adopt_baseline(game, _bid(regulation="11601000"), base)
    (game / "regulation.bin").write_bytes(b"1.17")
    heal.adopt_baseline(game, _bid(), base)
    assert {p.name for p in base.iterdir()} == {
        "regulation-11601000.bin", "regulation-11701000.bin"}


def test_a_missing_game_regulation_raises(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    with pytest.raises(HealError, match="regulation.bin"):
        heal.adopt_baseline(game, _bid(), tmp_path / "baselines")


def test_layout_gate_passes_when_every_stride_matches(monkeypatch):
    layouts = {"EquipParamWeapon.param": (1024, 3), "SpEffectParam.param": (512, 2)}
    monkeypatch.setattr(heal, "param_layouts", lambda blob: layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_layout_gate_names_the_table_and_mod_when_a_stride_moves(monkeypatch):
    def layouts(blob):
        if blob == b"base":
            return {"EquipParamWeapon.param": (1032, 3)}
        return {"EquipParamWeapon.param": (1024, 3)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("clevers", b"mod")])
    assert len(problems) == 1
    assert "clevers" in problems[0]
    assert "EquipParamWeapon.param" in problems[0]
    assert "1024" in problems[0] and "1032" in problems[0]


def test_layout_gate_catches_a_moved_paramdef_version(monkeypatch):
    def layouts(blob):
        return {"SpEffectParam.param": (512, 4 if blob == b"base" else 2)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("nofalldead", b"mod")])
    assert len(problems) == 1
    assert "paramdef" in problems[0]


def test_a_table_the_baseline_lacks_is_not_a_layout_problem(monkeypatch):
    # A mod may ship a table the baseline doesn't carry. That is a merge
    # question, not a transplant-safety one -- the gate must not claim it.
    def layouts(blob):
        return {} if blob == b"base" else {"Odd.param": (8, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("weird", b"mod")]) == ()


def test_layout_gate_reports_every_mod_not_just_the_first(monkeypatch):
    def layouts(blob):
        if blob == b"base":
            return {"A.param": (10, 1)}
        return {"A.param": (12, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    problems = heal.layout_gate(b"base", [("a", b"m1"), ("b", b"m2")])
    assert len(problems) == 2


def test_layout_gate_skips_a_table_the_baseline_cannot_read(monkeypatch):
    def layouts(blob):
        return {"Cutscene.param": None} if blob == b"base" else {"Cutscene.param": (16, 1)}
    monkeypatch.setattr(heal, "param_layouts", layouts)
    assert heal.layout_gate(b"base", [("clevers", b"mod")]) == ()


def test_layout_gate_skips_a_table_the_mod_cannot_read(monkeypatch):
    def layouts(blob):
        return {"Cutscene.param": (16, 1)} if blob == b"base" else {"Cutscene.param": None}
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
