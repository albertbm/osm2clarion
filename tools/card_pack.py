#!/usr/bin/env python3
"""Shared helpers for packing a .cmp/.ind pair and updating a card's MMAIN/editor reads.

Split out of a country build's own clean-card tool, since nothing here is country-specific.
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clarion_tile import walk  # noqa: E402

BLK = 65536


def wrap(data):
    """.cmp/.ind pair: zlib level 6 per 64 KiB block. An empty file is one empty block,
    the shape the card already boots for a 0-byte family."""
    blocks = [zlib.compress(data[i:i + BLK], 6) for i in range(0, len(data), BLK)] or [zlib.compress(b'', 6)]
    offs, o = [], 0
    for b in blocks:
        offs.append(o)
        o += len(b)
    c = b''.join(blocks)
    ind = struct.pack('<IIII', len(data), len(c), len(blocks), BLK) + b''.join(struct.pack('<I', x) for x in offs)
    return c, ind


def mmain(m, headers):
    """MMAIN001.DAT's level table holds, per section and level, the sum of the level-table
    counts of every map file - exact on the stock .066 card for sections 1, 2 and 4. So the
    counts are summed from the files on this card; a section with no files on it goes to 0.
    The db bounds and the NAVTEQ release string stay: writing the build's own box here makes
    the unit reset for a map version-up on every boot (measured), and both are the values
    every factory tile of this db cut carries."""
    tot = {}
    for h in headers:
        lv, _ = walk(h, 0x1ac, 4)
        for sid, rows in lv:
            for e in rows:
                tot[(sid, e[0], e[1])] = tot.get((sid, e[0], e[1]), 0) + struct.unpack('>H', e[2:])[0]
    m = bytearray(m)
    lv, _ = walk(m, 0x1ac, 4)
    p = 0x1ae
    for sid, rows in lv:
        p += 2
        for e in rows:
            struct.pack_into('>H', m, p + 2, tot.get((sid, e[0], e[1]), 0))
            p += 4
    return bytes(m)


def ed_read(ed, d, n):
    """A whole file's plain bytes, read through an already-open fatedit.Editor."""
    g = ed.find(d, n)
    fs = ed.fs
    out = b''.join(fs.rd(fs.coff(c), fs.csz) for c in fs.chain(g.first))
    return out[:g.size]
