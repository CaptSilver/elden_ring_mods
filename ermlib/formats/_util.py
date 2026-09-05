"""Helpers shared by the format parsers in this package.

Leading underscore because this isn't part of formats' public surface -- the
readers in this package import from it, nothing outside does.
"""


def read_utf16z(data, offset, error_cls, message):
    """Decode a null-terminated UTF-16LE string starting at `offset`.

    BND4 entry names, FMG strings and TPF texture names use the identical
    encoding and the identical truncation hazard (a buffer that ends
    mid-string), so the scan lives here once. Callers still need their own
    exception type, so it's injected rather than picking one module to own
    every meaning.
    """
    end = offset
    while data[end:end + 2] != b"\x00\x00":
        if end >= len(data):
            raise error_cls(message)
        end += 2
    try:
        return data[offset:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        # Decoded strict, so a lone surrogate lands here. UnicodeDecodeError is
        # not an ErmError, and the CLI catches nothing else.
        raise error_cls(f"string at {offset} is not valid UTF-16: {exc}") from exc
