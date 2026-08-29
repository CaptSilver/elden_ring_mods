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
