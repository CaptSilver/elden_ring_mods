CAVEAT = (
    "This audit flags careless tampering only. It CANNOT certify a save as "
    "legitimate: FromSoft does not publish detection criteria, and a careful "
    "edit stays within every value this checks."
)


class Finding:
    def __init__(self, severity, slot, message):
        self.severity = severity
        self.slot = slot
        self.message = message


class AuditResult:
    def __init__(self, findings):
        self.findings = findings
        self.caveat = CAVEAT

    @property
    def decisive(self):
        return [f for f in self.findings if f.severity == "decisive"]

    @property
    def tampered(self):
        return bool(self.decisive)


def audit_save(sf):
    findings = []
    for e in sf.entries:
        if not e.md5_ok:
            findings.append(Finding("decisive", e.index, f"entry {e.name} MD5 mismatch"))

    for ch in sf.characters:
        try:
            sd = sf.slot_data(ch.slot, ch.name)
        except Exception as exc:
            findings.append(Finding("decisive", ch.slot, f"slot {ch.slot} unparseable: {exc}"))
            continue
        for name, val in sd.stats.items():
            if val > 99:
                findings.append(Finding("decisive", ch.slot, f"{name} {val} > 99 (impossible)"))
        if sd.level > 713:
            findings.append(Finding("decisive", ch.slot, f"level {sd.level} > 713"))
        if sd.walk_cursor != sd.pgd_base:
            findings.append(Finding("decisive", ch.slot, "gaitem walk / stat scan desync"))
        if sd.common_count_field > 0xA80:
            findings.append(Finding("decisive", ch.slot, f"common item count {sd.common_count_field} > 0xA80"))
        if sd.key_count_field > 0x180:
            findings.append(Finding("decisive", ch.slot, f"key item count {sd.key_count_field} > 0x180"))
        if sd.corrupt_handles:
            findings.append(Finding("decisive", ch.slot, f"{sd.corrupt_handles} corrupt item handles"))
        for it in sd.items:
            if it.quantity > 999:
                findings.append(Finding("decisive", ch.slot, f"item {it.item_id} qty {it.quantity} > 999"))
                break
        if sum(sd.stats.values()) - 79 != sd.level:
            findings.append(Finding("suggestive", ch.slot, "level != sum(stats)-79 (edited stats?)"))
    return AuditResult(findings)
