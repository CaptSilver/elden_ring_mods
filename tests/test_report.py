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
