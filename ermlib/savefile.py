import hashlib
import struct

from .errors import ErmError


class NotAnEldenRingSave(ErmError):
    """File is not a PC Elden Ring BND4 save."""


PS_MAGIC = b"\xcb\x01\x9c\x2c"


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i64(b, o):
    return struct.unpack_from("<q", b, o)[0]


class Entry:
    def __init__(self, data, index):
        o = 0x40 + index * 0x20
        self.index = index
        self.size = _i64(data, o + 0x08)
        self.data_offset = _u32(data, o + 0x10)
        name_off = _u32(data, o + 0x14)
        end = data.index(b"\x00\x00", name_off)
        # UTF-16LE name, aligned to even length
        raw = data[name_off:end + (1 if (end - name_off) % 2 else 0)]
        self.name = raw.decode("utf-16-le", "ignore").rstrip("\x00")
        blob = data[self.data_offset:self.data_offset + self.size]
        self.stored_md5 = blob[:0x10]
        self.body = blob[0x10:]

    @property
    def md5_ok(self):
        return hashlib.md5(self.body).digest() == self.stored_md5


# --- profile summary (entry 10) ---
_PROFILE_BODY_FILE_BASE = 0x19003B0     # data_offset(0x19003A0) + 0x10 MD5
_ACTIVE_FILE = 0x1901D04
_REC0_FILE = 0x1901D0E
_REC_STRIDE = 0x24C


class Character:
    def __init__(self, slot, name, level, seconds, active):
        self.slot = slot
        self.name = name
        self.level = level
        self.seconds = seconds
        self.active = active


def _profile_local(file_off):
    return file_off - _PROFILE_BODY_FILE_BASE


class _CharactersMixin:
    @property
    def characters(self):
        body = self.profile_entry.body
        active_base = _profile_local(_ACTIVE_FILE)
        rec0 = _profile_local(_REC0_FILE)
        out = []
        for i in range(10):
            if body[active_base + i] != 1:
                continue
            b = rec0 + i * _REC_STRIDE
            name = body[b:b + 0x22].decode("utf-16-le", "ignore").split("\x00")[0]
            level = _u32(body, b + 0x22)
            seconds = _u32(body, b + 0x26)
            out.append(Character(i, name, level, seconds, active=True))
        return out


_STAT_NAMES = ["vigor", "mind", "endurance", "strength",
               "dexterity", "intelligence", "faith", "arcane"]
_INV_OFFSET = 0x3A4
_COMMON_CAP = 0xA80
_KEY_CAP = 0x180


class Item:
    __slots__ = ("category", "item_id", "quantity", "acq")

    def __init__(self, category, item_id, quantity, acq):
        self.category = category
        self.item_id = item_id
        self.quantity = quantity
        self.acq = acq


class SlotData:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _walk_gaitems(body):
    version = _u32(body, 0)
    n = 0x13FE if version <= 81 else 0x1400
    o = 0x20
    gmap = {}
    for _ in range(n):
        h = _u32(body, o)
        iid = _u32(body, o + 4)
        gmap[h] = iid
        top = h & 0xF0000000
        step = 8
        if h != 0 and top != 0xC0000000:
            step += 8
            if top == 0x80000000:
                step += 5
        o += step
    return gmap, o


def _find_pgd(body, name):
    wn = name.encode("utf-16-le")
    for base in range(0x20, min(len(body) - 0x100, 0x60000)):
        stats = [_u32(body, base + 0x34 + 4 * k) for k in range(8)]
        if not all(1 <= s <= 99 for s in stats):
            continue
        if _u32(body, base + 0x60) != sum(stats) - 79:
            continue
        if any(_u32(body, base + z) != 0 for z in (0x20, 0x30, 0x54, 0x58, 0x5c)):
            continue
        if wn[:8] not in body[base + 0x94:base + 0xD4]:
            continue
        return base
    return None


def _resolve(handle, gmap):
    top = handle & 0xF0000000
    if handle == 0:
        return None
    if top in (0xA0000000, 0xB0000000):
        return handle & 0x0FFFFFFF
    if top in (0x80000000, 0x90000000, 0xC0000000):
        iid = gmap.get(handle)
        return None if iid is None else iid & 0x0FFFFFFF
    return -1  # corrupt nibble


def _read_inventory_slot(body, offset, gmap):
    """Read one 12-byte {handle, qty, acq} inventory slot.

    Returns (item_or_None, is_corrupt). An empty or unresolvable handle yields
    (None, False); a handle with an unknown nibble yields (None, True) so both
    inventory sections count corruption identically.
    """
    h = _u32(body, offset)
    qty = _u32(body, offset + 4)
    acq = _u32(body, offset + 8)
    r = _resolve(h, gmap)
    if r == -1:
        return None, True
    if r is None:
        return None, False
    return Item(h >> 28, r, qty, acq), False


class _InventoryMixin:
    def slot_data(self, slot, name):
        body = self.slots[slot].body
        gmap, cursor = _walk_gaitems(body)
        pgd = _find_pgd(body, name)
        if pgd is None:
            raise NotAnEldenRingSave(f"could not locate PlayerGameData for slot {slot}")
        stats = {n: _u32(body, pgd + 0x34 + 4 * k) for k, n in enumerate(_STAT_NAMES)}
        level = _u32(body, pgd + 0x60)
        runes = _u32(body, pgd + 0x64)

        inv = pgd + _INV_OFFSET
        common_count = _u32(body, inv)
        items, corrupt = [], 0
        o = inv + 4
        for _ in range(_COMMON_CAP):
            item, is_corrupt = _read_inventory_slot(body, o, gmap)
            o += 12
            corrupt += is_corrupt
            if item is not None:
                items.append(item)
        key_count = _u32(body, o)
        o += 4
        keys = []
        for _ in range(_KEY_CAP):
            item, is_corrupt = _read_inventory_slot(body, o, gmap)
            o += 12
            corrupt += is_corrupt
            if item is not None:
                keys.append(item)

        return SlotData(
            stats=stats, level=level, runes=runes, pgd_base=pgd,
            walk_cursor=cursor, items=items, key_items=keys,
            common_count_field=common_count, key_count_field=key_count,
            corrupt_handles=corrupt,
        )


class SaveFile(_CharactersMixin, _InventoryMixin):
    def __init__(self, entries):
        self.entries = entries

    @classmethod
    def from_bytes(cls, data):
        if data[:4] == PS_MAGIC:
            raise NotAnEldenRingSave("PlayStation save (no per-entry MD5) — not supported")
        if data[:4] != b"BND4":
            raise NotAnEldenRingSave("not a BND4 Elden Ring save")
        try:
            count = _u32(data, 0x0C)
            entries = [Entry(data, i) for i in range(count)]
        except (struct.error, ValueError, IndexError) as exc:
            raise NotAnEldenRingSave(f"malformed/truncated save: {exc}") from exc
        return cls(entries)

    @property
    def slots(self):
        return self.entries[0:10]

    @property
    def profile_entry(self):
        return self.entries[10]

    @property
    def regulation_entry(self):
        return self.entries[11]

    @property
    def all_md5_ok(self):
        return all(e.md5_ok for e in self.entries)
