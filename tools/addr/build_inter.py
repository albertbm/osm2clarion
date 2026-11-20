#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One country's intersection search (INTER<nnn>.DAT) from the roads of our rebuilt tiles.

Two named roads meet where a drawn shape labelled with each has a graph node (a shape start
or a link end) on the same point of the same level-1 parcel. Each first street gets one
2ND-STREET record per crossing street, at the first such point in parcel order, so the
position is a point of both shapes and lands on the junction.

The card keeps its ICODE area tree. A street's ladder at a junction is the ladder of the
nearest record of the same name in the card's own INTER file within NEAR_KM, else the
ladder of the nearest record of any name: the tree has no geometry of its own.

    build_inter.py OUTDIR --profile NAME --card CARD.img --tiles TILE.DAT ...

Writes INTER<ordinal + 1>.DAT, as a .cmp/.ind pair when the card stores it packed, and
card.json naming what goes on the card.
"""
import argparse
import collections
import json
import math
import re
import struct
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / 'addr'))
import addr_links  # noqa: E402
import card_pack  # noqa: E402
import clarion_format  # noqa: E402
import clarion_profile  # noqa: E402
import fatread  # noqa: E402
import icode_io  # noqa: E402
import inter_io  # noqa: E402
import shape_index  # noqa: E402
from addr_resolve import code_rc  # noqa: E402

NEAR_KM = 5.0
SQUEEZE = math.cos(math.radians(62))


def card_file(fs, path):
    """(plain bytes, stored packed) of a card file that may be a .cmp/.ind pair."""
    try:
        return fs.read(path), False
    except FileNotFoundError:
        return card_pack.unwrap(fs.read(path + '.cmp'), fs.read(path + '.ind')), True


def point_ll(la, lo, x, y):
    """(lat, lon) of a point x, y (2048ths) inside the level-1 parcel la, lo."""
    row, col = code_rc(la, lo)
    return (row + y / 2048) / 48, (col + x / 2048) / 32 - 924


def km(a, b):
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(math.radians(a[0])))


def colon_names(roads, text):
    """{label spelling: name} of the road names that hold a colon, which a label cannot."""
    with open(roads, encoding='utf-8') as f:
        found = set(re.findall(r'"name": ("(?:[^"\\]|\\.)*")', f.read()))
    names = {text(json.loads(n).upper()) for n in found if ':' in n}
    return {shape_index.label_text(n): n for n in names}


def tile_nodes(path, colon=None):
    """{(la, lo, x, y): {name}} of every graph node of a named shape in one tile. `colon`
    gives a street its colon back."""
    colon = colon or {}
    d = open(path, 'rb').read()
    codec = clarion_format.of_header(d).codec
    out = collections.defaultdict(set)
    for (plain, bg) in shape_index.tile_records(d).values():
        code, _unit, shapes = shape_index.shapes_of(plain, bg, codec)
        w = struct.unpack('>%dH' % (len(plain) // 2), plain)
        for s in shapes:
            if not s['label'].startswith('1='):
                continue        # unnamed, or a road number alone
            name = shape_index.label_name(s['label'])
            name = colon.get(name, name)
            for word, (role, _link) in s['points'].items():
                if role != 'vertex':
                    out[code + (w[word] & 0x7ff, w[word + 1] & 0x7ff)].add(name)
    return out


class Ladders:
    """The ladder a street has at a point, from the card's own intersection records."""

    def __init__(self, inter):
        import numpy as np
        from shapely import STRtree, points
        self.np = np
        named = collections.defaultdict(list)
        pts, lvs = [], []
        for f in (inter_io.parse_first(r) for r in inter.first):
            for r in inter.second[f['second_at']:f['second_at'] + f['count']]:
                s = inter_io.parse_second(r)
                if code_rc(s['la'], s['lo']) is None:
                    continue
                p = point_ll(s['la'], s['lo'], s['x'], s['y'])
                for name, lv in ((f['name'], f['levels']), (s['name'], s['levels'])):
                    named[name].append((p, lv))
                    pts.append(p)
                    lvs.append(lv)
        self.byname = {n: (np.array([p for p, _ in v]), [lv for _, lv in v]) for n, v in named.items()}
        self.lvs = lvs
        # lon squeezed by cos 62 degrees, so the planar nearest is near enough the true one
        self.tree = STRtree(points([(lon * SQUEEZE, lat) for lat, lon in pts]))
        self.point = points

    def __call__(self, name, p):
        if name in self.byname:
            arr, lvs = self.byname[name]
            d = self.np.hypot((arr[:, 0] - p[0]) * 111.2, (arr[:, 1] - p[1]) * 111.2 * math.cos(math.radians(p[0])))
            i = int(d.argmin())
            if d[i] <= NEAR_KM:
                return lvs[i], 'name'
        return self.lvs[int(self.tree.nearest(self.point(p[1] * SQUEEZE, p[0])))], 'near'


def build(nodes, ladders, codec):
    """(keys, rows, first records, second records, stats) of the junctions in nodes."""
    pairs = {}
    how = collections.Counter()
    skipped = collections.Counter()
    cache = {}

    def ladder(name, key):
        if (name, key) not in cache:
            lv, h = ladders(name, point_ll(*key))
            cache[(name, key)] = lv
            how[h] += 1
        return cache[(name, key)]
    for key in sorted(nodes):
        names = sorted(nodes[key])
        if len(names) < 2:
            continue
        for a in names:
            for b in names:
                if a == b:
                    continue
                k = (a, ladder(a, key), b, ladder(b, key))
                if k not in pairs:
                    pairs[k] = key
    groups = collections.defaultdict(list)
    for (a, la, b, lb), key in pairs.items():
        groups[(a, la)].append((b, lb, key))

    def order(name, lv):
        return name.encode(codec), lv
    firsts, seconds = [], []
    for a, la in sorted(groups, key=lambda g: order(*g)):
        if not inter_io.search_key(a).isascii():
            skipped[a] += 1
            continue
        run = sorted(groups[(a, la)], key=lambda t: order(t[0], t[1]))
        firsts.append(inter_io.first_record(a, la, len(seconds), len(run), codec))
        for i, (b, lb, (pla, plo, x, y)) in enumerate(run):
            seconds.append(inter_io.second_record(b, lb, pla, plo, x, y, i > 0, codec))
    bykey = collections.defaultdict(list)
    for i, r in enumerate(firsts):
        f = inter_io.parse_first(r, codec)
        for u, later in inter_io.word_keys(inter_io.search_key(f['name'])):
            bykey[u].append((i, later, f['levels']))
    keys, rows = [], []
    for u in sorted(bykey):
        start = len(rows)
        for i, later, lv in sorted(bykey[u]):
            rows.append(inter_io.row(lv, i, later))
        keys.append((u, start, len(rows) - start))
    keys = [(u, f, n) for u, (_, f, n) in zip(addr_links.shortest_keys([k[0] for k in keys]), keys)]
    return keys, rows, firsts, seconds, dict(pairs=len(pairs), ladder=dict(how), skipped=sorted(skipped))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--profile', required=True)
    ap.add_argument('--card', required=True, help='the base card image; read for ICODE001.DAT and its INTER file')
    ap.add_argument('--tiles', nargs='+', required=True)
    ap.add_argument('--roads', help='roads.json of the rebuilt roads: its names with a colon keep it')
    a = ap.parse_args()
    P = clarion_profile.load(a.profile)
    fmt = P.fmt
    if not fmt.refer_v5:
        raise SystemExit('profile %s is format %s; INTER is written for X08-02 cards only' % (P.name, fmt.name))
    with fatread.Fat32(a.card) as fs:
        ic, _ = card_file(fs, '/REFER001/ICODE001.DAT')
        ordinal = icode_io.iso_ordinal(ic, P.country)
        name = 'INTER%03d.DAT' % (ordinal + 1)
        factory, packed = card_file(fs, '/REFER001/' + name)
    old = inter_io.Inter(factory)
    if old.area != ordinal:
        raise SystemExit('%s on the card is area %d, not country %d (ordinal %d)' % (name, old.area, P.country, ordinal))
    nodes = collections.defaultdict(set)
    colon = colon_names(a.roads, lambda s: fmt.text(s).decode(fmt.codec)) if a.roads else {}
    for t in a.tiles:
        for k, v in tile_nodes(t, colon).items():
            nodes[k] |= v
    keys, rows, firsts, seconds, st = build(nodes, Ladders(old), fmt.codec)
    if not firsts:
        raise SystemExit('no two named roads meet in the tiles')
    out = inter_io.assemble(old.prefix, old.df, keys, rows, firsts, seconds, ordinal, fmt.deflate)
    # what we wrote must read back as written
    back = inter_io.Inter(out)
    assert (back.keys, back.rows, back.first, back.second) == (keys, rows, firsts, seconds)

    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    (od / name).write_bytes(out)
    files = {'REFER001/' + name: name}
    if packed:
        c, i = card_pack.wrap(out)
        (od / (name + '.cmp')).write_bytes(c)
        (od / (name + '.ind')).write_bytes(i)
        files = {'REFER001/%s.%s' % (name, x): '%s.%s' % (name, x) for x in ('cmp', 'ind')}
    (od / 'card.json').write_text(json.dumps(dict(files=files, index={'REFER001:' + name: len(out)}), indent=1))
    (od / 'inter_skipped.json').write_text(json.dumps(st['skipped'], ensure_ascii=False, indent=1))
    print('%s: ordinal %d; %d graph nodes, %d where named roads meet; %d street pairs'
          % (name, ordinal, len(nodes), sum(len(v) > 1 for v in nodes.values()), st['pairs']))
    print('ladders: %s; left out (no typeable key): %d names' % (st['ladder'], len(st['skipped'])))
    print('%s %d B (factory %d B): %d keys, %d rows, %d first streets, %d crossings%s'
          % (name, len(out), len(factory), len(keys), len(rows), len(firsts), len(seconds), ', packed' if packed else ''))


if __name__ == '__main__':
    main()
