"""The Seamless Co-op release shapes several CLI test modules build the same way.

Kept in one place because production pins them: ermlib/install.py rewrites
SeamlessCoop/ersc_settings.ini and ermlib/me3profile.py points me3 at
SeamlessCoop/ersc.dll, both by hardcoded path, so an upstream layout change has
to land here too. tests/test_ersc_fixtures.py holds the archive and those two
paths together.
"""
import zipfile


def make_ersc_zip(path):
    """The shape of a real Seamless Co-op release archive."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ersc_launcher.exe", b"\x00")
        z.writestr("SeamlessCoop/ersc.dll", b"\x00")
        z.writestr("SeamlessCoop/ersc_settings.ini",
                   "[PASSWORD]\ncooppassword = \n[SAVE]\nsave_file_extension = co2\n")


def seed_profile(tmp_path, name="seamless-only"):
    """A one-mod profile installing seamless-coop straight into the game dir."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{name}.toml").write_text(
        f'name = "{name}"\n'
        'description = "test profile"\n'
        '\n'
        '[[mods]]\n'
        'id = "seamless-coop"\n'
        'source = "github"\n'
        'repo_id = 497113840\n'
        'kind = "coop-framework"\n'
        'install = "game"\n'
    )
