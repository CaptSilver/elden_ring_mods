"""Helpers shared by the format parsers in this package.

Leading underscore because this isn't part of formats' public surface --
only bnd4.py and fmg.py import from it.
"""


def read_utf16z(data, offset, error_cls, message):
    """Decode a null-terminated UTF-16LE string starting at `offset`.

    BND4 entry names and FMG strings use the identical encoding and the
    identical truncation hazard (a buffer that ends mid-string), so the scan
    lives here once. The two callers still need their own exception type,
    so it's injected rather than picking one module to own both meanings.
    """
    end = offset
    while data[end:end + 2] != b"\x00\x00":
        if end >= len(data):
            raise error_cls(message)
        end += 2
    return data[offset:end].decode("utf-16-le")
