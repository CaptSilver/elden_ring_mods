import tomllib
from pathlib import Path
from ermlib import state as state_mod
from ermlib.me3profile import reconcile, USER_MARKER


def _game(tmp_path):
    g = tmp_path / "Game"
    (g / "SeamlessCoop").mkdir(parents=True, exist_ok=True)
    (g / "SeamlessCoop" / "ersc.dll").write_bytes(b"x")
    return g


def test_reconcile_writes_header_and_supports(tmp_path):
    prof = reconcile({}, tmp_path / "tools" / "me3", _game(tmp_path))
    text = prof.read_text()
    assert 'profileVersion = "v1"' in text
    assert "[[supports]]" in text and 'game = "eldenring"' in text
    assert prof.name == "erm-coop.me3"


def test_reconcile_adds_ersc_native_only_when_seamless_installed(tmp_path):
    game = _game(tmp_path)
    me3 = tmp_path / "tools" / "me3"
    without = reconcile({}, me3, game).read_text()
    assert "[[natives]]" not in without
    with_sc = reconcile({"seamless-coop": {"files": []}}, me3, game).read_text()
    assert "[[natives]]" in with_sc
    assert "ersc.dll" in with_sc


def test_reconcile_lists_packages_sorted_by_id(tmp_path):
    s = {}
    state_mod.record_me3_package(s, "zebra", "1", "z.zip", "tools/me3/mods/zebra")
    state_mod.record_me3_package(s, "alpha", "1", "a.zip", "tools/me3/mods/alpha")
    text = reconcile(s, tmp_path / "tools" / "me3", _game(tmp_path)).read_text()
    assert text.index('id = "alpha"') < text.index('id = "zebra"')
    assert "path = 'mods/alpha/'" in text


def test_reconcile_is_deterministic(tmp_path):
    s = {}
    state_mod.record_me3_package(s, "b", "1", "b.zip", "tools/me3/mods/b")
    state_mod.record_me3_package(s, "a", "1", "a.zip", "tools/me3/mods/a")
    me3 = tmp_path / "tools" / "me3"
    first = reconcile(s, me3, _game(tmp_path)).read_text()
    second = reconcile(s, me3, _game(tmp_path)).read_text()
    assert first == second


def test_reconcile_escapes_apostrophe_in_game_path(tmp_path):
    game = _game(tmp_path / "O'Brien")
    me3 = tmp_path / "tools" / "me3"
    text = reconcile({"seamless-coop": {"files": []}}, me3, game).read_text()
    parsed = tomllib.loads(text)
    expected_ersc = str((game / "SeamlessCoop" / "ersc.dll").resolve())
    assert parsed["natives"][0]["path"] == expected_ersc


def test_reconcile_uses_clean_literal_quoting_without_apostrophe(tmp_path):
    game = _game(tmp_path)
    me3 = tmp_path / "tools" / "me3"
    text = reconcile({"seamless-coop": {"files": []}}, me3, game).read_text()
    expected_ersc = str((game / "SeamlessCoop" / "ersc.dll").resolve())
    assert f"path = '{expected_ersc}'" in text
    parsed = tomllib.loads(text)
    assert parsed["natives"][0]["path"] == expected_ersc


def test_reconcile_preserves_user_region(tmp_path):
    me3 = tmp_path / "tools" / "me3"
    prof = reconcile({}, me3, _game(tmp_path))
    text = prof.read_text()
    assert USER_MARKER in text
    prof.write_text(text + "\n[[packages]]\nid = \"my-hand-add\"\npath = 'mods/mine/'\n")
    after = reconcile({}, me3, _game(tmp_path)).read_text()
    assert "my-hand-add" in after
