#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow every street record of a card's ADDRE files into the card's road tiles.

A name record's (lat, lon) words are the parcel code of a level-1 0x00fa drawn-shape
record; each reference group (tag, flag, v) points at the x word `unit + v` of a point on
the street's shape (REPORT-kanren in leaf-is-map). For each record this reads the card's
own tile, finds that record, and checks every group:

    lands      word unit + v is a point x word of a shape
    named      the shape's label names the street
    flag       0x40 on a graph node (shape start or link end), 0x00 on an interior vertex
    order      tags 2/3 ascend, 4/5 descend, between hops; one shape per run
    hops       a tag 0x40/0x50/0x60/0x70 moves one parcel N/E/S/W and v counts there

Reads tiles with clarion_tile.read_tile, so it is for our own cards: whole parcels only.

    addr_resolve.py CARD.img [--verbose]
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addre_io, clarion_bg, fatread, shape_index

HOPS = {0x40: (1, 0), 0x50: (0, 1), 0x60: (-1, 0), 0x70: (0, -1)}


def unpack_cmp(cmp, ind):
    """A .cmp/.ind pair inflated: the .ind lists each zlib block's offset from 0x10 on."""
    size = struct.unpack_from('<I', ind, 0)[0]
    offs = [struct.unpack_from('<I', ind, i)[0] for i in range(0x10, len(ind), 4)]
    out = b''.join(zlib.decompress(cmp[a:b]) for a, b in zip(offs, offs[1:] + [len(cmp)]) if a < len(cmp))
    assert len(out) == size, (len(out), size)
    return out


def code_rc(la, lo):
    """Absolute level-1 (row, col) of a whole-parcel code; None for a sub-parcel."""
    if (la >> 2) & 3 != 3 or (lo >> 2) & 3 != 3: return None
    c = ((la >> 15) & 1) << 8 | lo >> 8
    row = ((la >> 8) & 0x7f) << 6 | ((la >> 4) & 15) << 2 | (la & 3)
    col = c << 6 | ((lo >> 4) & 15) << 2 | (lo & 3)
    return row, col


class Card:
    def __init__(self, img):
        self.fs = fatread.Fat32(img).__enter__()
        self.files = {p.strip('/'): (sz, c) for p, sz, c, _ in self.fs.walk() if sz is not None}
        self.tiles = {}

    def read(self, path):
        sz, c = self.files[path]
        return self.fs.f.read_clusters(c, sz) if sz else b''

    def addre(self):
        out = {}
        for p in sorted(self.files):
            d, n = os.path.split(p)
            if d != 'REFER001' or not n.startswith('ADDRE'): continue
            if n.endswith('.DAT'): out[n] = self.read(p)
            elif n.endswith('.DAT.cmp'): out[n[:-4]] = unpack_cmp(self.read(p), self.read(p[:-4] + '.ind'))
        return out

    def records(self, row, col):
        """tile_records of the tile holding a level-1 parcel."""
        y, x = row // 32, col // 32
        for p in self.files:
            d, n = os.path.split(p)
            if d != 'MAPAL001' or len(n) != 12 or n[0] != 'B' or n[3] != 'R': continue
            yb, xb = clarion_bg.tile_cell(n[:8])
            if yb <= y < yb + 8 and xb <= x < xb + 8:
                if n not in self.tiles: self.tiles[n] = shape_index.tile_records(self.read(p))
                return n, self.tiles[n]
        return None, {}


def resolve(card, rec, street):
    """[(group, outcome dict)] and a list of failures for one name record."""
    rc = code_rc(rec['lat'], rec['lon'])
    if rc is None: return [], ['sub-parcel code %04x %04x not handled' % (rec['lat'], rec['lon'])]
    row, col = rc; out = []; bad = []; seg = []; segs = [seg]
    for g in rec['refs']:
        tag, flag, v = g
        if tag >= 0x40:
            if tag not in HOPS: bad.append('hop tag 0x%02x not decoded' % tag); continue
            dr, dc = HOPS[tag]; row, col = row + dr, col + dc; seg = []; segs.append(seg)
        tile, recs = card.records(row, col)
        code = clarion_bg.parcel_code(1, row, col)
        if code not in recs:
            bad.append('group %s: no 0x00fa record %04x %04x' % (g, *code)); continue
        _, unit, shapes = shape_index.shapes_of(*recs[code])
        w = unit + v
        s = next((s for s in shapes if s['at'] <= w < s['at'] + s['length']), None)
        role = s['points'].get(w) if s else None
        o = dict(tile=tile, code=code, word=w, shape=s and s['at'], label=s and s['label'],
                 role=role and role[0], link=role and role[1])
        out.append((g, o)); seg.append((g, o))
        if role is None: bad.append('group %s: word %d is not a point x word' % (g, w)); continue
        parts = {p.strip().upper() for p in shape_index.label_name(s['label']).split(':')}
        if street.upper() not in parts: bad.append('group %s: shape label %r' % (g, s['label']))
        if (flag == 0x40) != (role[0] in ('start', 'end')): bad.append('group %s: flag 0x%02x on a %s' % (g, flag, role[0]))
    for sg in segs:
        for (ga, oa), (gb, ob) in zip(sg, sg[1:]):
            if oa['shape'] != ob['shape']: bad.append('groups %s %s: two shapes' % (ga, gb))
            up = ga[0] & 0xf in (2, 3) and gb[0] & 0xf in (2, 3)
            down = ga[0] & 0xf in (4, 5) and gb[0] & 0xf in (4, 5)
            if not ((up and gb[2] > ga[2]) or (down and gb[2] < ga[2]) or ga[0] >= 0x40 or gb[0] >= 0x40):
                bad.append('groups %s %s: order does not match the tags' % (ga, gb))
    return out, bad


def main(argv):
    if not argv: sys.exit(__doc__)
    card = Card(argv[0]); verbose = '--verbose' in argv
    total = ok = 0
    for fn, d in card.addre().items():
        a = addre_io.Addre(d); last = None; n = good = 0
        for r in a.names:
            rec = addre_io.parse_name_record(r)
            street = rec['name'] if rec['name'] is not None else last
            last = street
            out, bad = resolve(card, rec, street)
            n += 1; good += not bad
            if bad or verbose:
                print('  %-24s %04x %04x %s' % (street, rec['lat'], rec['lon'], ' '.join('(%d,0x%02x,%d)' % g for g in rec['refs'])))
                for g, o in out:
                    print('      %-16s %s %04x %04x word %d: %s of link %s, shape @%s %r'
                          % (g, o['tile'], *o['code'], o['word'], o['role'], o['link'], o['shape'], o['label']))
                for b in bad: print('      FAIL ' + b)
        print('%s: %d of %d street records resolve' % (fn, good, n))
        total += n; ok += good
    print('all: %d of %d (%s)' % (ok, total, 'PASS' if total and ok == total else 'FAIL'))
    return 0 if total and ok == total else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
