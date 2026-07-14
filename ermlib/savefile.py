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


class SaveFile(_CharactersMixin):
    def __init__(self, entries):
        self.entries = entries

    @classmethod
    def from_bytes(cls, data):
        if data[:4] == PS_MAGIC:
            raise NotAnEldenRingSave("PlayStation save (no per-entry MD5) — not supported")
        if data[:4] != b"BND4":
            raise NotAnEldenRingSave("not a BND4 Elden Ring save")
        count = _u32(data, 0x0C)
        entries = [Entry(data, i) for i in range(count)]
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
