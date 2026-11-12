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
from tile_header import BOUNDS, BOUNDS2  # noqa: E402

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


def unwrap(cmp_, ind):
    """The plain bytes of a .cmp/.ind pair."""
    n, _, blocks, _ = struct.unpack_from('<IIII', ind)
    offs = list(struct.unpack_from('<%dI' % blocks, ind, 16)) + [len(cmp_)]
    out = b''.join(zlib.decompress(cmp_[offs[i]:offs[i + 1]]) for i in range(blocks))
    assert len(out) == n, (len(out), n)
    return out


def stamp_text(version):
    return ('DATA VERSION ' + version).encode('utf-16-be')


def restamped(files, old, new):
    """{card path: (bytes, None, index size)} of a card's files (path -> bytes) with every
    `old` DATA VERSION stamp written as `new`, packed files unpacked and repacked; and the paths
    that carried no stamp. ICODE is left out: each unit builds its own."""
    a, b = stamp_text(old), stamp_text(new)
    exp, bare = {}, []
    for p, data in sorted(files.items()):
        if p.endswith(('.ind', '/ICODE001.DAT')):
            continue
        packed = p.endswith('.cmp')
        plain = unwrap(data, files[p[:-4] + '.ind']) if packed else data
        if a not in plain:
            bare.append(p)
        if a == b or a not in plain:
            exp[p] = (data, None, len(plain))
            if packed:
                exp[p[:-4] + '.ind'] = (files[p[:-4] + '.ind'], None, len(plain))
            continue
        plain = plain.replace(a, b)
        if packed:
            c, i = wrap(plain)
            exp[p], exp[p[:-4] + '.ind'] = (c, None, len(plain)), (i, None, len(plain))
        else:
            exp[p] = (plain, None, len(plain))
    return exp, bare


def mmain(m, headers):
    """MMAIN001.DAT's level table holds, per section and level, the sum of the level-table
    counts of every map file - exact on the stock .066 card for sections 1, 2 and 4. So the
    counts are summed from the files on this card; a section with no files on it goes to 0.
    The db box at 0x184 (lon part repeated at 0x5d0) is taken from the map files too: the
    unit derives tile cells from MMAIN's box, so it must be the box the tiles and LEV3456R
    were cut on. The ZE0/ZE1 bases already carry that box; the ME0 base carries the stock
    European one (lon -1200..4080), and keeping it left the Iceland cells unreachable
    (blank map at a 3D fix). Writing the region's own small box instead makes
    the unit reset for a map version-up on every boot (measured), so the box is the db
    cut's, never the build area's. The NAVTEQ release string stays."""
    tot = {}
    boxes = set()
    for h in headers:
        boxes.add(h[BOUNDS:BOUNDS + 8])
        lv, _ = walk(h, 0x1ac, 4)
        for sid, rows in lv:
            for e in rows:
                tot[(sid, e[0], e[1])] = tot.get((sid, e[0], e[1]), 0) + struct.unpack('>H', e[2:])[0]
    if len(boxes) > 1:
        raise ValueError('map files disagree on the db box: %s' % sorted(b.hex() for b in boxes))
    m = bytearray(m)
    if boxes:
        box = boxes.pop()
        m[BOUNDS:BOUNDS + 8] = box
        m[BOUNDS2:BOUNDS2 + 4] = box[2:6]   # lat min, lon min
    lv, _ = walk(m, 0x1ac, 4)
    p = 0x1ae
    for sid, rows in lv:
        p += 2
        for e in rows:
            struct.pack_into('>H', m, p + 2, tot.get((sid, e[0], e[1]), 0))
            p += 4
    return bytes(m)


def mmain_card(m, headers, nhouse):
    """mmain, with section 5 level 0 counting the card's HOUSE001 files when it has any."""
    mm = bytearray(mmain(m, headers))
    if nhouse:
        p = 0x1ae
        for sid, rows in walk(mm, 0x1ac, 4)[0]:
            p += 2
            for e in rows:
                if (sid, e[0]) == (5, 0):
                    struct.pack_into('>H', mm, p + 2, nhouse)
                p += 4
    return bytes(mm)


def ed_read(ed, d, n):
    """A whole file's plain bytes, read through an already-open fatedit.Editor."""
    g = ed.find(d, n)
    fs = ed.fs
    out = b''.join(fs.rd(fs.coff(c), fs.csz) for c in fs.chain(g.first))
    return out[:g.size]
