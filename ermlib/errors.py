class ErmError(Exception):
    """Base for all erm errors; message is safe to print to the user."""


class IntegrityError(ErmError):
    """A downloaded or installed file failed a hash/verify check."""


class PathError(ErmError):
    """A required Steam/game/save path could not be located."""


class SafetyError(ErmError):
    """A ban-relevant precondition was violated (e.g. EAC armed with mods)."""


class NetworkError(ErmError):
    """A GitHub API/download call failed (network error or bad response shape)."""
