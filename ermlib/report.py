import json
import sys

_RANK = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
_ICON = {"ok": "✓", "info": "•", "warn": "!", "fail": "✗"}
# Warnings and failures get ANSI colour on an interactive terminal so they don't
# scroll past unnoticed: warn = red, fail = bold red. Only emitted when stdout is
# a TTY (not piped/redirected/captured-under-tests) and never for --json.
_COLOR = {"warn": "\033[31m", "fail": "\033[1;31m"}
_RESET = "\033[0m"


def _line(level, msg, color):
    line = f"{_ICON[level]} {msg}"
    if color and level in _COLOR:
        line = f"{_COLOR[level]}{line}{_RESET}"
    return line


def _want_color():
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class Report:
    def __init__(self, stream=False):
        self.items = []
        # Print each item the moment it lands, for a command slow enough that
        # holding everything back to the end reads as a hang (a fetch pulling
        # gigabytes). render() then has nothing left to say, so whoever prints
        # the report checks `stream` first. Never set in --json mode: that has
        # to come out as one document.
        self.stream = stream

    def _add(self, level, msg):
        self.items.append((level, msg))
        if self.stream:
            print(_line(level, msg, _want_color()))

    def ok(self, msg): self._add("ok", msg)
    def info(self, msg): self._add("info", msg)
    def warn(self, msg): self._add("warn", msg)
    def fail(self, msg): self._add("fail", msg)

    @property
    def worst_level(self):
        worst = "ok"
        for level, _ in self.items:
            if _RANK[level] > _RANK[worst]:
                worst = level
        return worst

    @property
    def exit_code(self):
        return 1 if self.worst_level == "fail" else 0

    def to_dict(self):
        """The report as plain data, so a caller can nest it inside a bigger
        document instead of printing a second one after this one."""
        return {
            "worst": self.worst_level,
            "items": [{"level": lv, "message": m} for lv, m in self.items],
        }

    def render(self, as_json=False, color=None):
        if as_json:
            return json.dumps(self.to_dict())
        if color is None:
            color = _want_color()
        return "\n".join(_line(lv, m, color) for lv, m in self.items)
