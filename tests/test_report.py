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
    text = r.render(as_json=False)
    lines = text.split("\n")
    assert len(lines) == 2
    assert "EAC armed" in text
    assert lines[0] == "✓ game found"
    assert lines[1] == "✗ EAC armed"
