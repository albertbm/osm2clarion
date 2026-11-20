#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow every street record of a card's ADDRE files into the card's road tiles.

A name record's (lat, lon) words are the parcel code of a level-1 0x00fa drawn-shape
record; each reference group (tag, flag, v) points at the x word `unit + v` of a point on
the street's shape. For each record this reads the card's
own tile, finds that record, and checks every group:

    lands      word unit + v is a point x word of a shape
    named      the shape's label names the street
    flag       0x40 on a graph node (shape start or link end), 0x00 on an interior vertex
    order      inside a parcel the groups ascend to a tag 2/3, descend to a 4/5; one shape there
    hops       a tag 0x40/0x50/0x60/0x70 moves one parcel N/E/S/W and v counts there
    link       the two groups inside a parcel span one link, with no node between them
    ends       a record starts and ends on a node, not where a parcel line cuts its link
    across     a hop goes on from the same place on the parcel line
    style      on a tile that gives every link its style word, the link's header holds one
    items      at most one house-number item a side, the right side (0x54) first
    row        a range row's low and high numbers are those of its record's items
    joined     a street's ranges of one parity run on, each starting two above the one
               before: 98.1% of the factory's, pass at 90% of a file's

Reads tiles with clarion_tile.read_tile, so it is for our own cards: whole parcels only.
--only checks the named files, for a card that keeps factory address files beside ours.

    addr_resolve.py CARD.img [--verbose] [--only ADDRE007.DAT ...]
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addre_io, clarion_bg, clarion_format, fatread, shape_index

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
        self.codec = {}
        self.styled = {}

    def read(self, path):
        sz, c = self.files[path]
        return self.fs.f.read_clusters(c, sz) if sz else b''

    def addre(self, only=()):
        out = {}
        for p in sorted(self.files):
            d, n = os.path.split(p)
            if d != 'REFER001' or not n.startswith('ADDRE'): continue
            if only and n.split('.DAT')[0] + '.DAT' not in only: continue
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
                if n not in self.tiles:
                    d = self.read(p)
                    self.tiles[n] = shape_index.tile_records(d)
                    self.codec[n] = clarion_format.of_header(d).codec
                    self.styled[n] = clarion_format.of_header(d).style_every_link
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
        _, unit, shapes = shape_index.shapes_of(*recs[code], card.codec[tile])
        w = unit + v
        s = next((s for s in shapes if s['at'] <= w < s['at'] + s['length']), None)
        role = s['points'].get(w) if s else None
        o = dict(tile=tile, code=code, word=w, shape=s and s['at'], label=s and s['label'],
                 role=role and role[0], link=role and role[1], points=s and s['points'])
        if role:
            x, y = (c & 0x7ff for c in struct.unpack_from('>2H', recs[code][0], 2 * w))
            # the far line is 2047 on this side and 0 on the other
            o['at'] = (col * 2048 + (2048 if x == 2047 else x), row * 2048 + (2048 if y == 2047 else y))
            o['line'] = x in (0, 2047) or y in (0, 2047)
            o['styled'] = bool(struct.unpack_from('>H', recs[code][0], 2 * w + 2)[0] & 0x2000)
        out.append((g, o)); seg.append((g, o))
        if role is None: bad.append('group %s: word %d is not a point x word' % (g, w)); continue
        parts = {p.strip().upper() for p in shape_index.label_name(s['label']).split(':')}
        if shape_index.label_text(street.upper()) not in parts: bad.append('group %s: shape label %r' % (g, s['label']))
        if (flag == 0x40) != (role[0] in ('start', 'end')): bad.append('group %s: flag 0x%02x on a %s' % (g, flag, role[0]))
    if out and len(out) == len(rec['refs']):
        for g, o in (out[0], out[-1]):
            if o.get('line'): bad.append('group %s: the record ends on a parcel line' % (g,))
        for (ga, oa), (gb, ob) in zip(out, out[1:]):
            if gb[0] >= 0x40 and 'at' in oa and 'at' in ob and oa['at'] != ob['at']:
                bad.append('groups %s %s: the hop does not go on from the same place' % (ga, gb))
    for sg in segs:
        if len(sg) == 2 and sg[0][1]['shape'] == sg[1][1]['shape'] and all(o['role'] for _, o in sg):
            a, b = sorted((o for _, o in sg), key=lambda o: o['word'])
            if any(a['word'] < w < b['word'] and r[0] != 'vertex' for w, r in a['points'].items()):
                bad.append('groups %s %s span more than one link' % (sg[0][0], sg[1][0]))
            if card.styled[a['tile']] and not a['styled']:
                bad.append('groups %s %s: the link header has no style word' % (sg[0][0], sg[1][0]))
        for (ga, oa), (gb, ob) in zip(sg, sg[1:]):
            if oa['shape'] != ob['shape']: bad.append('groups %s %s: two shapes' % (ga, gb))
            # the second group's tag says which way the pair runs; a hop group has no say
            ka, kb = ({2: 'up', 3: 'up', 4: 'down', 5: 'down'}.get(g[0]) for g in (ga, gb))
            if kb is None or (gb[2] > ga[2]) != (kb == 'up') or gb[2] == ga[2] or not (ka == kb or ga[0] >= 0x40):
                bad.append('groups %s %s: order does not match the tags' % (ga, gb))
    return out, bad


def layout(rec, rows):
    """Failures of a name record's house-number items and of the range rows that point at it."""
    bad = []
    try:
        items = addre_io.parse_items(rec['items'])
    except ValueError:
        return ['items do not parse: %s' % rec['items'].hex()]
    sides = [side & 0x0c for side, _, _ in items]
    if sides not in ([], [4], [8], [4, 8]):
        bad.append('items for sides %s' % ' '.join('0x%02x' % side for side, _, _ in items))
    nums = [n for _, first, last in items for n in (first, last)]
    for r in rows:
        if r['rel'] != 0x7f7f and items and (r['lo'], r['hi']) != (min(nums), max(nums)):
            bad.append('row %d-%d over items %d-%d' % (r['lo'], r['hi'], min(nums), max(nums)))
    return bad


JOINED = 0.9      # the share of same-parity neighbours that must join; the factory's is 0.981


class Streets:
    """What a file's records must agree on beyond their own bytes."""

    def __init__(self):
        self.runs, self.pairs, self.joined = {}, 0, 0

    def add(self, rec, street):
        """Failures of one name record, taken in file order."""
        bad = []
        try:
            items = addre_io.parse_items(rec['items'])
        except ValueError:
            return bad       # layout has said so
        if rec['name'] is not None:
            self.close()
        for _, first, last in items:
            if first % 2 == last % 2:
                self.runs.setdefault(first % 2, []).append((min(first, last), max(first, last)))
        return bad

    def close(self):
        for run in self.runs.values():
            run.sort()
            self.pairs += len(run) - 1
            self.joined += sum(b[0] - a[1] == 2 for a, b in zip(run, run[1:]))
        self.runs = {}

    def ok(self):
        self.close()
        return self.joined >= JOINED * self.pairs


def main(argv):
    if not argv: sys.exit(__doc__)
    card = Card(argv[0]); verbose = '--verbose' in argv
    only = argv[argv.index('--only') + 1:] if '--only' in argv else ()
    total = ok = 0; whole = True
    for fn, d in card.addre(only).items():
        a = addre_io.Addre(d); last = None; n = good = 0
        rows = {}; streets = Streets()
        for i in range(a.nsrec):
            row = addre_io.parse_range_record(a.srec(i))
            rows.setdefault(row['name_idx'], []).append(row)
        for i, r in enumerate(a.names):
            rec = addre_io.parse_name_record(r, a.fmt)
            street = rec['name'] if rec['name'] is not None else last
            last = street
            out, bad = resolve(card, rec, street)
            bad += layout(rec, rows.get(i, ()))
            bad += streets.add(rec, street)
            n += 1; good += not bad
            if bad or verbose:
                print('  %-24s %04x %04x %s' % (street, rec['lat'], rec['lon'], ' '.join('(%d,0x%02x,%d)' % g for g in rec['refs'])))
                for g, o in out:
                    print('      %-16s %s %04x %04x word %d: %s of link %s, shape @%s %r'
                          % (g, o['tile'], *o['code'], o['word'], o['role'], o['link'], o['shape'], o['label']))
                for b in bad: print('      FAIL ' + b)
        print('%s: %d of %d street records resolve' % (fn, good, n))
        joined = streets.ok()
        print('%s: %d of %d same-parity neighbours join (%s at %d%%)' % (fn, streets.joined, streets.pairs, 'PASS' if joined else 'FAIL', 100 * JOINED))
        total += n; ok += good; whole &= joined
    good = total and ok == total and whole
    print('all: %d of %d (%s)' % (ok, total, 'PASS' if good else 'FAIL'))
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
