#pragma once
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <stdint.h>
#include <x86intrin.h>

typedef unsigned char byte;
typedef unsigned char uint8;
typedef unsigned int uint32;
typedef uint64_t uint64;
typedef int64_t int64;
typedef signed int int32;
typedef unsigned short uint16;
typedef signed short int16;
typedef unsigned int uint;

// MSVC intrinsic shims (GCC/Clang builtins)
static inline unsigned char _BitScanReverse(unsigned long *index, unsigned long mask) {
    if (!mask) return 0;
    *index = 31 - __builtin_clz((unsigned int)mask);
    return 1;
}
#define _byteswap_ushort(x)  __builtin_bswap16(x)
#define _byteswap_ulong(x)   __builtin_bswap32(x)
#define _byteswap_uint64(x)  __builtin_bswap64(x)
static inline unsigned char _BitScanForward(unsigned long *index, unsigned long mask) {
    if (!mask) return 0;
    *index = __builtin_ctz((unsigned int)mask);
    return 1;
}
#define __forceinline inline __attribute__((always_inline))
