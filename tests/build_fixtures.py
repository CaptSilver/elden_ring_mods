"""A complete BuildId, for the modules that need an identity to hand around.

BuildId deliberately has no field defaults -- classify() reads the disagreements
between its three identity sources, and a defaulted field would let production
build a half-populated identity that looks like agreement. So every construction
has to spell all five fields, and the convenience of not doing that by hand
belongs here rather than on the type.
"""
from ermlib.gamebuild import BuildId

_BASE = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
             steam_buildid="23850278", regulation_sha="a" * 64)


def build_id(**over):
    return BuildId(**{**_BASE, **over})
