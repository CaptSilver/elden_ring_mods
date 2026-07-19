from ermlib.report import Report


def test_report_tracks_worst_level_and_exit_code():
    r = Report()
    r.ok("game found")
    r.warn("password blank")
    assert r.worst_level == "warn"
    assert r.exit_code == 0
    r.fail("EAC armed with proxy dll")
    assert r.worst_level == "fail"
    assert r.exit_code == 1


def test_report_json_render_is_parseable():
    import json
    r = Report()
    r.ok("a")
    r.fail("b")
    data = json.loads(r.render(as_json=True))
    assert data["worst"] == "fail"
    assert {"level": "fail", "message": "b"} in data["items"]


def test_report_info_does_not_escalate_worst_level():
    r = Report()
    r.info("just so you know")
    assert r.worst_level == "ok"
    assert r.exit_code == 0
    assert ("info", "just so you know") in r.items


def test_report_text_render_uses_icons_and_newline_join():
    r = Report()
    r.ok("game found")
    r.fail("EAC armed")
    text = r.render(as_json=False, color=False)   # pin: deterministic under `pytest -s`
    lines = text.split("\n")
    assert len(lines) == 2
    assert "EAC armed" in text
    assert lines[0] == "✓ game found"
    assert lines[1] == "✗ EAC armed"


def test_render_colors_warn_red_and_fail_bold_red_when_color_true():
    # Warnings/failures scroll past unnoticed in a wall of output — colour them so
    # they stand out. warn = red, fail = bold red; ok/info stay uncoloured.
    r = Report()
    r.ok("good"); r.info("note"); r.warn("careful"); r.fail("broke")
    lines = r.render(color=True).split("\n")
    assert lines[0] == "✓ good"                     # ok: no colour
    assert lines[1] == "• note"                     # info: no colour
    assert lines[2] == "\033[31m! careful\033[0m"   # warn: red
    assert lines[3] == "\033[1;31m✗ broke\033[0m"   # fail: bold red


def test_render_color_false_emits_no_ansi():
    r = Report()
    r.warn("careful"); r.fail("broke")
    text = r.render(color=False)
    assert "\033[" not in text
    assert text == "! careful\n✗ broke"


def test_render_json_never_colored_even_when_color_true():
    import json
    r = Report()
    r.warn("careful")
    out = r.render(as_json=True, color=True)
    assert "\033[" not in out
    assert json.loads(out)["items"][0]["message"] == "careful"   # message stays clean


def test_render_color_defaults_follow_stdout_tty(monkeypatch):
    # No explicit color arg -> follow whether stdout is a TTY, so an interactive
    # terminal gets red warnings but pipes/redirects/tests stay plain.
    import sys

    class FakeOut:
        def __init__(self, tty): self._tty = tty
        def isatty(self): return self._tty
        def write(self, *a): pass
        def flush(self): pass

    r = Report(); r.warn("careful")
    monkeypatch.setattr(sys, "stdout", FakeOut(True))
    tty_out = r.render()
    monkeypatch.setattr(sys, "stdout", FakeOut(False))
    plain_out = r.render()
    assert "\033[31m" in tty_out
    assert "\033[" not in plain_out
