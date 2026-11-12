#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add a dealer/POI icon point to an already-built tile, at one or more pre-measured
positions.

A layer-25 point with no side entry draws the badge (measured on a ZE1 emulator with an
0x8040..0x807f icon-code sweep). The point joins whatever layer 25 already holds, so any
facility icons bg_tiles --pois wrote in the same parcels stay; replacing the layer instead
would drop them. Run twice, it adds nothing.

    poi_badge.py IN.DAT OUT.DAT --icon 0x806c --pos LEVEL,Y0,X0,ROW,COL,PX,PY [--pos ...]

Positions are a build's own measured parcel location, not derived from a coordinate here -
pass one --pos per section-1 level the badge should appear at.
"""
import argparse
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile
import clarion_bg as cb


def get_record(t, level, y0, x0, row, col):
    for sec in t['sections']:
        if sec['id'] != 1:
            continue
        for L in sec['levels']:
            if L['level'] != level:
                continue
            for D in L['dirs']:
                if D['y'] == y0 and D['x'] == x0:
                    k = 2 * col + 1
                    sl = D['slots'][row]
                    i = next(i for i, r in enumerate(sl['recs']) if r['k'] == k)
                    return D, sl, i, sl['recs'][i]
    raise SystemExit('no level-%d directory at y=%d x=%d' % (level, y0, x0))


def plain_of(r):
    raw = r['raw']
    return zlib.decompress(raw) if raw[:2] == b'\x58\x85' else raw


def write_record(sl, i, r, new_plain):
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(new_plain) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    r['raw'] = comp
    r['words'] = len(new_plain) // 2
    k = r['k']
    body = bytearray(sl['body'])
    struct.pack_into('>II', body, 8 * k, len(comp) // 2, len(new_plain) // 2)
    sl['body'] = bytes(body)


def mark(rec, icon, px, py):
    """Layer 25 gains the icon point unless it already has it; True when added."""
    g = rec['layers'].get(25)
    if g and 'shapes' in g and any(s['type'] == icon and s['pts'][0] == (px, py) for s in g['shapes']):
        return False
    cb.add_point(rec, icon, px, py)
    return True


def parse_pos(text):
    level, y0, x0, row, col, px, py = (int(x) for x in text.split(','))
    return level, y0, x0, row, col, px, py


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--icon', required=True, help='point type code, e.g. 0x806c')
    ap.add_argument('--pos', action='append', required=True, metavar='LEVEL,Y0,X0,ROW,COL,PX,PY')
    a = ap.parse_args(argv)
    icon = int(a.icon, 0)

    d = open(a.src, 'rb').read()
    t = clarion_tile.read_tile(d)
    for text in a.pos:
        level, y0, x0, row, col, px, py = parse_pos(text)
        D, sl, i, r = get_record(t, level, y0, x0, row, col)
        rec = cb.parse_record(plain_of(r))
        added = mark(rec, icon, px, py)
        write_record(sl, i, r, cb.build_record(rec))
        print('level %d y=%d x=%d row=%d col=%d: 0x%04x at (%d,%d) %s; layer 25 holds %d points'
              % (level, y0, x0, row, col, icon, px, py, 'added' if added else 'already there',
                 len(rec['layers'][25]['shapes'])))
    out = clarion_tile.write_tile(t)
    open(a.dst, 'wb').write(out)
    print('tile %d -> %d bytes' % (len(d), len(out)))


if __name__ == '__main__':
    main(sys.argv[1:])
