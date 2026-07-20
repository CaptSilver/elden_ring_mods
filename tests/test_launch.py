from pathlib import Path

from ermlib import launch


def test_repo_root_holds_the_me3_profile():
    # Derived from __file__, not cwd, so the printed command is right no matter
    # where erm was invoked from.
    assert (launch.REPO_ROOT / "ermlib" / "launch.py").exists()
    assert launch.PROFILE == launch.REPO_ROOT / "tools" / "me3" / "erm-coop.me3"
    assert launch.PROFILE.is_absolute()


def test_find_me3_prefers_path_lookup(monkeypatch, tmp_path):
    onpath = tmp_path / "me3"
    onpath.write_text("")
    monkeypatch.setattr(launch.shutil, "which", lambda n: str(onpath))
    assert launch.find_me3() == onpath.resolve()


def test_find_me3_falls_back_to_local_bin(monkeypatch, tmp_path):
    fallback = tmp_path / "me3"
    fallback.write_text("")
    monkeypatch.setattr(launch.shutil, "which", lambda n: None)
    monkeypatch.setattr(launch, "ME3_FALLBACK", fallback)
    assert launch.find_me3() == fallback.resolve()


def test_find_me3_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda n: None)
    monkeypatch.setattr(launch, "ME3_FALLBACK", tmp_path / "nope")
    assert launch.find_me3() is None


def test_me3_command_is_absolute_and_ends_with_command_token(tmp_path):
    cmd = launch.me3_command(Path("/opt/me3"), tmp_path / "p.me3")
    assert cmd.startswith("/opt/me3 launch -p /")
    # Steam appends this text as argv to the game exe unless %command% appears.
    assert cmd.endswith("# %command%")


def test_me3_command_quotes_paths_containing_spaces(tmp_path):
    weird = tmp_path / "a b" / "erm-coop.me3"
    cmd = launch.me3_command(Path("/opt/me3"), weird)
    assert f"'{weird}'" in cmd


def _variants(tmp_path, me3_bin="/opt/me3", reshade=False, packages=False, profile=None):
    prof = profile if profile is not None else tmp_path / "erm-coop.me3"
    return launch.build_variants(
        Path(me3_bin) if me3_bin else None, reshade, packages, prof)


def test_build_variants_always_emits_every_command(tmp_path):
    # The whole point: no branching on what's installed.
    for reshade in (True, False):
        for packages in (True, False):
            v = _variants(tmp_path, reshade=reshade, packages=packages)
            assert v["me3"]["plain"] and v["me3"]["reshade"]
            assert v["ersc"]["plain"] and v["ersc"]["reshade"]
            assert v["validator"] == launch.LAUNCH_VALIDATOR


def test_build_variants_commands_do_not_vary_with_detected_state(tmp_path):
    # The emitted commands must be identical for every combination of what
    # happens to be installed — those two values annotate the output, they
    # never select it. Comparing only (False, False) against (True, True)
    # would miss an implementation that diverged on the mixed corners.
    baseline = _variants(tmp_path, reshade=False, packages=False)
    for reshade in (True, False):
        for packages in (True, False):
            v = _variants(tmp_path, reshade=reshade, packages=packages)
            for key in ("me3", "ersc", "validator"):
                assert v[key] == baseline[key], (reshade, packages, key)


def test_build_variants_reshade_forms_prepend_the_override(tmp_path):
    v = _variants(tmp_path)
    assert v["ersc"]["plain"] == launch.LAUNCH_OPTION
    assert v["ersc"]["reshade"] == launch.RESHADE_ENV + launch.LAUNCH_OPTION
    assert v["me3"]["reshade"] == launch.RESHADE_ENV + v["me3"]["plain"]


def test_build_variants_reports_observations(tmp_path):
    v = _variants(tmp_path, reshade=True, packages=True)
    assert v["reshade_installed"] is True
    assert v["me3_packages"] is True
    assert v["profile_exists"] is False

    prof = tmp_path / "erm-coop.me3"
    prof.write_text("")
    v = _variants(tmp_path, profile=prof)
    assert v["profile_exists"] is True


def test_build_variants_me3_is_none_when_binary_missing(tmp_path):
    v = _variants(tmp_path, me3_bin=None)
    assert v["me3"] is None
    # ersc still works without me3 installed.
    assert v["ersc"]["plain"] == launch.LAUNCH_OPTION


def test_me3_command_defaults_to_the_module_profile(monkeypatch, tmp_path):
    # A `profile=PROFILE` default would bind at def time and ignore this patch.
    patched = tmp_path / "patched.me3"
    monkeypatch.setattr(launch, "PROFILE", patched)
    assert str(patched) in launch.me3_command(Path("/opt/me3"))


def test_build_variants_defaults_to_the_module_profile(monkeypatch, tmp_path):
    patched = tmp_path / "patched.me3"
    patched.write_text("")
    monkeypatch.setattr(launch, "PROFILE", patched)
    v = launch.build_variants(Path("/opt/me3"), False, False)
    assert str(patched) in v["me3"]["plain"]
    assert v["profile_exists"] is True


def test_render_contains_every_command_in_one_output(tmp_path):
    out = launch.render(_variants(tmp_path))
    assert "Steam → ELDEN RING → Properties → Launch Options" in out
    assert launch.LAUNCH_OPTION in out
    assert launch.RESHADE_ENV + launch.LAUNCH_OPTION in out
    assert launch.LAUNCH_VALIDATOR in out
    assert "launch -p" in out
    assert "# %command%" in out
    assert "Dual GPU" in out


def test_render_annotates_me3_packages_without_hiding_commands(tmp_path):
    present = launch.render(_variants(tmp_path, packages=True))
    absent = launch.render(_variants(tmp_path, packages=False))
    assert "me3 packages present" in present
    assert "no me3 packages" in absent
    for out in (present, absent):
        assert launch.LAUNCH_OPTION in out
        assert "launch -p" in out


def test_render_annotates_reshade_without_hiding_variants(tmp_path):
    on = launch.render(_variants(tmp_path, reshade=True))
    off = launch.render(_variants(tmp_path, reshade=False))
    assert "ReShade is installed on this machine" in on
    assert "ReShade is not installed on this machine" in off
    # Both forms print either way — that's what makes the output copyable
    # for a machine you're not on.
    for out in (on, off):
        assert launch.RESHADE_ENV + launch.LAUNCH_OPTION in out


def test_render_warns_and_omits_commands_when_me3_missing(tmp_path):
    out = launch.render(_variants(tmp_path, me3_bin=None))
    assert "me3 is not installed on this machine" in out
    # No broken-but-plausible command.
    assert "launch -p" not in out
    assert launch.LAUNCH_OPTION in out


def test_render_warns_when_profile_absent_but_still_shows_commands(tmp_path):
    out = launch.render(_variants(tmp_path))
    assert "does not exist yet" in out
    assert "erm apply" in out
    assert "launch -p" in out

    prof = tmp_path / "erm-coop.me3"
    prof.write_text("")
    assert "does not exist yet" not in launch.render(_variants(tmp_path, profile=prof))
