# ooz (vendored)

Kraken decompressor from https://github.com/powzix/ooz, GPL-3.0-or-later,
Copyright (C) 2016 Powzix. Upstream has not moved since 2019.

Vendored rather than submoduled because we have to modify it — see below.
A submodule would pin someone else's tree, so carrying these patches would
mean maintaining a fork of a dead repo or a build-time patch step that can fail.

## Local modifications

**`compat.h`** — ooz is MSVC-flavored and will not compile under g++ without
shims for `_BitScanReverse`, `_BitScanForward`, `__forceinline`, and several
MSVC typedefs. GCC's `x86intrin.h` supplies `_rotl`.

**`kraken_lib.cpp`** — adds `ooz_decompress_seekchunked`, the entry point we
actually call.

**`stdafx.h`** — every vendored `.cpp` still `#include`s this (an MSVC
precompiled-header convention). Upstream's version pulls in `<Windows.h>` and
`<tchar.h>`, so it's replaced with a two-line shim that just forwards to
`compat.h`.

## The seek-chunk gotcha

Stock `Kraken_Decompress` fails on **every** FromSoftware DCX file, vanilla
included, after exactly one 256 KB block. This is not a build error — a correct
build decodes the whole Silesia corpus byte-perfect, including a 51 MB file.

FromSoft sets `seekChunkReset=true`, so each 256 KB chunk is coded
independently and back-references must be chunk-relative. The fix is to loop
over `Kraken_DecodeStep` with a per-chunk destination base rather than calling
`Kraken_Decompress` once:

    if (!Kraken_DecodeStep(dec, dst + written, 0, dst_len, src, src_len)) return -1;

If you are debugging a decode that dies after 262144 bytes, this is why.

## Build

Built on demand by `ermlib/formats/ooz.py` to `tools/ooz/libooz.so`, keyed on a
sha256 over these sources. Needs only `g++` (present in the Bazzite base image
and the fedora distrobox). The build is reproducible — a clean-room rebuild
produced a byte-identical .so.
