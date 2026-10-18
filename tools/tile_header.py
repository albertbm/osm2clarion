#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 0x1000-byte header of an X08-03-00 map tile, written from its parts.

A factory header holds five things that change between tiles or between database cuts:
the DATA VERSION string (0xe0), the NAVTEQ release string (0x3b0), the database box (0x184,
repeated in part at 0x5d0), the level table (0x1ac) and the box table (0x2ac). The section
table at 0x800 is filled by clarion_tile.write_tile. Everything else is the same byte for
byte in all 156 tile headers of the 2014, 2015 and 2016 European cards, and is written here
from constants. check() rebuilds a header from its five fields alone, so that claim can be
tested on any card.

    tile_header.py --check FILE...      rebuild each header and compare
"""
import struct
import sys

IDENT = {0x00: 'CLARION', 0x40: 'ZENRIN', 0x80: 'CLA-NAVI06-01',
         0xa0: 'FORMAT VERSION X08-03-00', 0x162: 'V06.00.00'}
# raw constants by offset, outside the 0xff fill
CONSTS = {0x160: '1600', 0x182: '24ff', 0x18c: '3f' * 20, 0x3ac: '0100',
          0x4ed: '01011000000120', 0x5cc: '80000000', 0x5d4: '0b0b'}
ZERO = ((0x4ac, 0x4ed), (0x52c, 0x5cc), (0x5d6, 0x800), (0x800 + 6 * 300, 0x1000))
DATA_VERSION, SOURCE, BOUNDS, BOUNDS2 = 0xe0, 0x3b0, 0x184, 0x5d0
LEVEL_OFF, BOX_OFF, SECT_OFF, SECT_SLOTS = 0x1ac, 0x2ac, 0x800, 300

# section id -> its (level, type) rows, in the order every factory tile lists them
LEVELS = {1: [(6, 0x80), (5, 0x90), (4, 0x91), (3, 0x91), (2, 0x91), (1, 0x92)],
          2: [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)],
          4: [(1, 0x93)]}


def author(level_groups, box_rows, db_bounds, data_version, source=None):
    """level_groups: [(section id, [(level, type, count), ...]), ...]
    box_rows:     [(section id, [(level, flag, (y1, y0, x0, x1) arc-minutes), ...]), ...]
    db_bounds:    (lat max, lat min, lon min, lon max) in arc-minutes
    data_version: 'yy/mm/dd/nn', checked by the firmware against the NAND
    source:       the release string at 0x3b0, or None to leave it out
    The section table is left as 300 empty slots for write_tile to fill."""
    h = bytearray(b'\xff' * 0x1000)
    for a, b in ZERO:
        h[a:b] = bytes(b - a)
    for off, text in IDENT.items():
        s = text.encode('utf-16-be')
        h[off:off + len(s)] = s
    for off, hexs in CONSTS.items():
        b = bytes.fromhex(hexs)
        h[off:off + len(b)] = b
    s = ('DATA VERSION ' + data_version).encode('utf-16-be')
    assert len(s) == 48, data_version
    h[DATA_VERSION:DATA_VERSION + 48] = s
    if source:
        s = source.encode('utf-16-be')
        h[SOURCE:SOURCE + len(s)] = s
    struct.pack_into('>4h', h, BOUNDS, *db_bounds)
    struct.pack_into('>2h', h, BOUNDS2, db_bounds[1], db_bounds[2])

    p = LEVEL_OFF
    struct.pack_into('>H', h, p, len(level_groups)); p += 2
    for sid, rows in level_groups:
        h[p], h[p + 1] = sid, len(rows); p += 2
        for lvl, typ, cnt in rows:
            struct.pack_into('>BBH', h, p, lvl, typ, cnt); p += 4
    assert p <= BOX_OFF, 'level table runs into the box table'

    p = BOX_OFF
    struct.pack_into('>H', h, p, len(box_rows)); p += 2
    for sid, rows in box_rows:
        h[p], h[p + 1] = sid, len(rows); p += 2
        for lvl, flag, box in rows:
            struct.pack_into('>BB4h', h, p, lvl, flag, *box); p += 10
    assert p <= 0x3ac, 'box table runs into the constant at 0x3ac'

    for i in range(SECT_SLOTS):
        struct.pack_into('>IH', h, SECT_OFF + 6 * i, 0xffffffff, 0)
    return h


def level_groups(t):
    """The level table for a clarion_tile object: each section's rows with its directory
    counts. A section's rows are the format's full list for that id, so a level with no
    directories still appears with count 0, as in every factory tile."""
    out = []
    for sec in t['sections']:
        have = {L['level']: len(L['dirs']) for L in sec['levels']}
        out.append((sec['id'], [(lvl, typ, have.get(lvl, 0)) for lvl, typ in LEVELS[sec['id']]]))
    return out


def fields(h):
    """The five variable fields of a header, as author() takes them, plus the section
    table entries write_tile would have written."""
    def walk(off, elen):
        n = struct.unpack_from('>H', h, off)[0]; p = off + 2; out = []
        for _ in range(n):
            sid, cnt = h[p], h[p + 1]; p += 2
            out.append((sid, [h[p + i * elen:p + (i + 1) * elen] for i in range(cnt)])); p += cnt * elen
        return out
    lv = [(sid, [(e[0], e[1], struct.unpack('>H', e[2:])[0]) for e in rows]) for sid, rows in walk(LEVEL_OFF, 4)]
    bx = [(sid, [(e[0], e[1], struct.unpack('>4h', e[2:])) for e in rows]) for sid, rows in walk(BOX_OFF, 10)]
    dv = bytes(h[DATA_VERSION:DATA_VERSION + 48]).decode('utf-16-be')
    assert dv.startswith('DATA VERSION '), dv
    src = bytes(h[SOURCE:SOURCE + 22])
    src = None if src == b'\xff' * 22 else src.decode('utf-16-be')
    sect, p = [], SECT_OFF
    while True:
        a, c = struct.unpack_from('>IH', h, p); p += 6
        if a == 0xffffffff:
            break
        sect.append((a, c))
    return dict(level_groups=lv, box_rows=bx, db_bounds=struct.unpack_from('>4h', h, BOUNDS),
                data_version=dv[13:], source=src), sect


def check(h):
    """Rebuild a header from its fields; return the offsets that differ."""
    f, sect = fields(h)
    b = author(**f)
    for i, (a, c) in enumerate(sect):
        struct.pack_into('>IH', b, SECT_OFF + 6 * i, a, c)
    return [i for i in range(0x1000) if b[i] != h[i]]


if __name__ == '__main__':
    if sys.argv[1:2] != ['--check']:
        sys.exit(__doc__)
    bad = 0
    for fn in sys.argv[2:]:
        with open(fn, 'rb') as fh:
            h = fh.read(0x1000)
        d = check(h)
        bad += bool(d)
        print(fn, 'exact' if not d else 'DIFFERS at %s' % ' '.join('%#x' % i for i in d[:12]))
    print('%d of %d headers rebuilt exactly' % (len(sys.argv) - 2 - bad, len(sys.argv) - 2))
    sys.exit(1 if bad else 0)
