import json
import sys

_RANK = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
_ICON = {"ok": "✓", "info": "•", "warn": "!", "fail": "✗"}
# Warnings and failures get ANSI colour on an interactive terminal so they don't
# scroll past unnoticed: warn = red, fail = bold red. Only emitted when stdout is
# a TTY (not piped/redirected/captured-under-tests) and never for --json.
_COLOR = {"warn": "\033[31m", "fail": "\033[1;31m"}
_RESET = "\033[0m"


class Report:
    def __init__(self):
        self.items = []

    def _add(self, level, msg):
        self.items.append((level, msg))

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

    def render(self, as_json=False, color=None):
        if as_json:
            return json.dumps({
                "worst": self.worst_level,
                "items": [{"level": lv, "message": m} for lv, m in self.items],
            })
        if color is None:
            try:
                color = sys.stdout.isatty()
            except (AttributeError, ValueError):
                color = False
        lines = []
        for lv, m in self.items:
            line = f"{_ICON[lv]} {m}"
            if color and lv in _COLOR:
                line = f"{_COLOR[lv]}{line}{_RESET}"
            lines.append(line)
        return "\n".join(lines)
