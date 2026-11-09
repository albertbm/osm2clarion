#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wrap a decompressed REFER file back into a .cmp/.ind pair.

zlib level 6 per 64 KiB block, which reproduces the factory COORD001 pair byte for byte.
"""

import zlib, struct
BLK = 65536
def wrap(data, level=6):
    """Clarion .cmp/.ind pair: zlib level 6 per 64 KiB block; .ind is
    [u32 unc][u32 comp][u32 blocks][u32 block size][u32 offset per block]."""
    blocks = [zlib.compress(data[i:i+BLK], level) for i in range(0, len(data), BLK)]
    offs, o = [], 0
    for b in blocks:
        offs.append(o); o += len(b)
    cmp_ = b''.join(blocks)
    ind = struct.pack('<IIII', len(data), len(cmp_), len(blocks), BLK) + b''.join(struct.pack('<I', x) for x in offs)
    return cmp_, ind
