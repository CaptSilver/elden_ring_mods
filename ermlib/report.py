import json

_RANK = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
_ICON = {"ok": "✓", "info": "•", "warn": "!", "fail": "✗"}


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

    def render(self, as_json=False):
        if as_json:
            return json.dumps({
                "worst": self.worst_level,
                "items": [{"level": lv, "message": m} for lv, m in self.items],
            })
        return "\n".join(f"{_ICON[lv]} {m}" for lv, m in self.items)
