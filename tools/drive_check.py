#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive a few hundred long routes over a card's own road graph and run the unit's lookups
for every link they use, at every level that has the link.

    drive_check.py --card A.img [--routes N] [--seed S] [--json]
    drive_check.py --tiles TILE ... --lev LEV3456R.DAT [--routes N] [--seed S] [--json]

A route is the quickest path between two level-1 nodes far apart, by the travel times in
the graph. The unit plans the ends of such a route at level 1 and the rest at levels 2, 4
and 5, then needs two things of every link it planned with:

  drawn     a road shape of the link's class and id in its parcel at its level, in the
            layer the unit reads for the class (Navi.exe 0x516604); else the route line
            has a gap there
  expands   the level-1 shapes naming the link walk from its first node, each starting
            where the last ended (Navi.exe 0x34d2b8); else the close-up line stops there

A route also may not fall off a level inside a junction. Where it runs from one link of a
level to another over up to three level-1 links of the level's classes that name no link,
the two links must meet at a node, or the level's own links between them are driven in
their place; with neither, the unit cannot plan the way the road runs. And a long route
with no link of a level at all means level 1 names none.

Apart from the routes, every level-2 edge triple must name the far node's own block (cell
word, level-1 child), as all 31,826 on the stock QY7 Finland tiles do. Ours named the far
node's level-2 index wherever a road crossed a tile line, and 300 routes never saw it.

Every miss is listed with its place. Exit status 1 when a driven link is not drawn or does
not expand, when a tenth of the routes use no link of a level the card has, or when an edge
triple is wrong.

No link may fail to expand because every one that did was a fault: our Iceland card had 4
of 5,595 at level 2 and 5 of 1,505 at level 4, from junctions sharing a level-2 unit,
crossings beside a parcel corner and dips over a level-2 parcel line. Factory B50R068R
under the factory coarse file has all 41,347 driven links drawn and 11 of 5,043 not
expanding at level 2. At levels 4 and 5 its walks stop short far more often (1.5% and
11%), which this reader does not explain, so the factory is no guide there.
"""
import argparse
import collections
import heapq
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import card_check
import clarion_bg
import fa_encode
import rd_check
import rd_encode
from tile_scan import TileScan

JUNCTION = 3                          # level-1 links a junction may hide from a level; more is a route on other roads
OFF_LEVEL = 0.1                       # share of the routes that may use no link of a level the card has


def layer_of(cls):
    """The shape layer the unit looks a link of this class up in (table at Navi.exe 0xf46a0)."""
    return 7 if cls <= 2 else 6 if cls <= 7 else 5 if cls == 8 else 4 if cls == 9 else 0


def load_level2_shapes(paths):
    """{level-2 parcel: plain 0x00fa record} over a set of tiles."""
    out = {}
    for p in paths:
        with TileScan(p) as t:
            for code, rec in rd_check.shape_records(t, 2).items():
                try:
                    out[clarion_bg.parcel_rowcol(2, *code)] = rec.plain()
                except Exception:
                    continue
    return out


class Card:
    """What the lookups need of a card, read with card_check's loaders."""

    def __init__(self, tiles, lev, names=None):
        self.L1, self.L2, self.SH1, self.tile_of = {}, {}, {}, {}
        for t, name in zip(tiles, names or tiles):
            l1, l2, sh1 = card_check.load_road_tiles([t])
            self.L1.update(l1), self.L2.update(l2), self.SH1.update(sh1)
            name = os.path.basename(name)[:-4] if isinstance(name, str) else '?'
            self.tile_of.update((pc, name) for pc in l2)
        self.SH2 = load_level2_shapes(tiles)
        levd = card_check.load_lev(lev)
        self.lev = {4: levd.get(4, {}), 5: levd.get(5, {})}
        self.origin = levd.get('origin')
        self.coarse = card_check.load_lev_shapes(lev)


def parent(level, pc, origin):
    """The parcel at this level over a level-1 parcel."""
    if level == 2:
        return pc[0] // 4, pc[1] // 4
    span = card_check.COARSE_SPAN[level]
    return (pc[0] - origin[0] * 32) // span, (pc[1] - origin[1] * 32) // span


def level1_shapes(card):
    """{(parcel, class, link id): (in its class's layer, {level: link key above})} of the
    level-1 shape links."""
    out = {}
    for pc, w, shapes in card_check.road_shapes(card.SH1):
        for S in shapes:
            q = S['at'] + 6 + (w[S['at'] + 3] & 3)
            above = {}
            for u in w[q:q + (w[S['at']] >> 13)]:
                level = card_check.UPPER_CODE.get(u & 0xe000)
                if level == 2 or (level and card.origin is not None):
                    above[level] = parent(level, pc, card.origin) + (S['cls'], u & 0x1fff)
            for lid in S['links']:
                out[pc, S['cls'], lid & 0x1fff] = (S['layer'] == layer_of(S['cls']), above)
    return out


def drawn_links(card):
    """{level: {link key: what is wrong with its shape, or None}} for levels 2, 4 and 5."""
    out = {2: {}, 4: {}, 5: {}}
    for pc, pl in card.SH2.items():
        try:
            shapes = fa_encode.parse_record(pl, 2)[2]
        except Exception:
            continue
        for S in shapes:
            for lid in S['links']:
                out[2][pc + (S['cls'], lid & 0x1fff)] = None if S['layer'] == layer_of(S['cls']) else 'shape in the wrong layer'
    for level in (4, 5):
        for par, (a, b) in card.lev[level].items():
            ends = collections.defaultdict(dict)
            for nd in a['nodes']:
                for e in nd['entries']:
                    if not e['cls'] & 0x8000:
                        ends[e['cls'] & 0xf, e['id'] & 0x1fff][bool(e['id'] & 0x2000)] = (nd['x'], nd['y'])
            for layer, cls, links, above, named in card.coarse.get(level, {}).get(par, ()):
                for lid, first, last, way, npts in links:
                    g = ends.get((cls, lid), {})
                    out[level][par + (cls, lid)] = ('shape in the wrong layer' if layer != layer_of(cls)
                                                    else 'shape of more than %d points' % card_check.LINK_POINTS if npts > card_check.LINK_POINTS
                                                    else 'shape not from node to node' if (g.get(False), g.get(True)) != (first, last) else None)
    return out


def walks(card):
    """{level: {link key: how the walk down its level-1 shapes ends}} for levels 2, 4 and 5."""
    out = {2: {}, 4: {}, 5: {}}
    loaded = {pc for pc, w, shapes in card_check.road_shapes(card.SH1)}
    for pc2, cls, lid, status in card_check.upper_link_walks(card.L1, card.L2, card.SH1, loaded):
        out[2][pc2 + (cls, lid)] = status
    if card.lev[4] and card.origin is not None:
        for level, par, cls, lid, status in card_check.coarse_link_walks(card.L1, card.L2, card.SH1, card.lev[4], card.lev[5], card.origin, loaded):
            out[level][par + (cls, lid)] = status
    return out


def link_nodes(card):
    """{level: {link key: the places of its nodes}} for levels 2, 4 and 5, a node on a parcel
    line at the same place from both sides."""
    out = {}
    for level, recs in ((2, card_check.L2nodes(card.L2)), (4, {pc: a for pc, (a, b) in card.lev[4].items()}),
                        (5, {pc: a for pc, (a, b) in card.lev[5].items()})):
        at = out[level] = collections.defaultdict(set)
        for pc, a in recs.items():
            for nd in a['nodes']:
                place = pc[1] * 2048 + card_check.far_edge(nd['x']), pc[0] * 2048 + card_check.far_edge(nd['y'])
                for e in nd['entries']:
                    if not e['cls'] & 0x8000:
                        at[pc + (e['cls'] & 0xf, e['id'] & 0x1fff)].add(place)
    return out


def edge_triples(L1, L2, tile_of=None):
    """Every level-2 part-B edge triple against the far node's own block, which is what the
    factory writes (stock QY7 Finland: 31,826 of 31,826). Returns (Counter, per-tile Counters,
    notes for each wrong triple)."""
    st, per, notes = collections.Counter(), collections.defaultdict(collections.Counter), []
    blocks = {pc: {x['upper'] & 0x3fff: x for x in b['nodes']} for pc, (a, b) in L2.items()}
    for pc, (a, b) in sorted(L2.items()):
        T = per[(tile_of or {}).get(pc, '?')]
        for k, nd in enumerate(a['nodes']):
            blk = blocks[pc].get(k)
            if blk is None:
                continue
            for j, (e, (w0, w2, w4)) in enumerate(zip(nd['entries'], blk['entries'])):
                if not e['cls'] & 0x8000:
                    continue
                T['L2 edge triples'] += 1
                dr, dc = card_check.SIDE[e['cls'] & 0xe000]
                fpc, other = (pc[0] + dr, pc[1] + dc), e['other'] & 0x3fff
                if fpc not in L2:
                    T['L2 edge triples off the card'] += 1
                    continue
                fb = blocks[fpc].get(other)
                what = []
                if fb is None:
                    what.append('far node %d has no block' % other)
                    T['L2 edge triples far node with no block'] += 1
                elif (w2, w4) != (fb['parcel'], fb['child']):
                    what.append('names (%04x, %d), far block is (%04x, %d)%s' % (w2, w4, fb['parcel'], fb['child'],
                                ', the far level-2 index' if w4 == other else ''))
                    T['L2 edge triples wrong'] += 1
                far1 = L1.get((fpc[0] * 4 + (w2 >> 14), fpc[1] * 4 + (w2 >> 6 & 3)))
                if far1 is not None and w4 >= len(far1['nodes']):
                    what.append('child %d past the %d nodes of its level-1 record' % (w4, len(far1['nodes'])))
                    T['L2 edge triples child out of range'] += 1
                if what:
                    notes.append('level 2 edge triple in parcel %s node %d entry %d, far %s node %d: %s'
                                 % (pc, k, j, fpc, other, '; '.join(what)))
    for T in per.values():
        st.update(T)
    return st, dict(per), notes


def where(node):
    """(lat, lon) of a level-1 node given as (parcel, x, y)."""
    pc, x, y = node
    per_lon, per_lat = clarion_bg.PARCELS_PER_DEG[1]
    return (pc[0] + y / 2048) / per_lat, (pc[1] + x / 2048) / per_lon - 924


def km(p, q):
    return 111.2 * math.hypot(p[0] - q[0], (p[1] - q[1]) * math.cos(math.radians(p[0])))


class Graph:
    """The level-1 graph as arrays: node i sits at place[i], and out[i] lists (node, tenths
    of a second, link key or None for the step over a parcel line)."""

    def __init__(self, L1):
        index = {}
        for pc in sorted(L1):
            for k in range(len(L1[pc]['nodes'])):
                index[pc, k] = len(index)
        self.place = [None] * len(index)
        self.out = [[] for _ in index]
        for (pc, k), i in index.items():
            nd = L1[pc]['nodes'][k]
            self.place[i] = where((pc, nd['x'], nd['y']))
            for e in nd['entries']:
                if e['cls'] & 0x8000:
                    far = card_check.cross(L1, pc, e)
                    if far is not None:
                        self.out[i].append((index[far], 0, None))
                elif (pc, e['other'] & 0x3fff) in index:
                    self.out[i].append((index[pc, e['other'] & 0x3fff], max(1, rd_encode.unscaled(e['a'], rd_encode.TIME_UNITS)),
                                        (pc, e['cls'] & 0xf, e['id'] & 0x1fff)))

    def tree(self, start):
        """Quickest-path tree from one node: {node: (node before, link key)}."""
        best, before, heap = {start: 0}, {start: (None, None)}, [(0, start)]
        while heap:
            t, i = heapq.heappop(heap)
            if t > best[i]:
                continue
            for j, dt, link in self.out[i]:
                if t + dt < best.get(j, 1 << 60):
                    best[j] = t + dt
                    before[j] = (i, link)
                    heapq.heappush(heap, (t + dt, j))
        return before

    def routes(self, count, seed=1, per=12):
        """[(start, end, [(node the link is left from, link key)])] for `count` routes, `per` from each start.

        The ends are at least a quarter of the map's width apart, drawn parcel by parcel so
        towns do not take them all."""
        rng = random.Random(seed)
        if not self.place:
            return []
        by_parcel = collections.defaultdict(list)
        for i, p in enumerate(self.place):
            by_parcel[int(p[0] * 12), int(p[1] * 8)].append(i)
        cells = sorted(by_parcel)
        pick = lambda: rng.choice(by_parcel[rng.choice(cells)])
        lats, lons = [p[0] for p in self.place], [p[1] for p in self.place]
        far = km((min(lats), min(lons)), (max(lats), max(lons))) / 4
        out = []
        for _ in range(4 * count):
            if len(out) >= count:
                break
            start = pick()
            before = self.tree(start)
            ends = [j for j in (pick() for _ in range(20 * per)) if j in before and km(self.place[start], self.place[j]) >= far]
            for end in ends[:min(per, count - len(out))]:
                path, i = [], end
                while before[i][0] is not None:
                    if before[i][1] is not None:
                        path.append((before[i][0], before[i][1]))
                    i = before[i][0]
                out.append((start, end, path[::-1]))
        return out


def drive(card, count=300, seed=1):
    """Run the lookups over `count` routes on a card, then check its edge triples."""
    r = judge(Graph(card.L1), level1_shapes(card), drawn_links(card), walks(card), link_nodes(card), count, seed)
    return add_edge_triples(r, *edge_triples(card.L1, card.L2, card.tile_of))


def add_edge_triples(r, st, per, notes):
    """Fold the edge triple counts into a drive result; any wrong one fails it."""
    bad = st['L2 edge triples wrong'] + st['L2 edge triples child out of range'] + st['L2 edge triples far node with no block']
    for k in ('L2 edge triples', 'L2 edge triples wrong', 'L2 edge triples child out of range',
              'L2 edge triples far node with no block', 'L2 edge triples off the card'):
        r['counts'][k] = st[k]
    tiles = ['%s %d of %d wrong, %d out of range' % (t, T['L2 edge triples wrong'], T['L2 edge triples'], T['L2 edge triples child out of range'])
             for t, T in sorted(per.items()) if T['L2 edge triples wrong'] or T['L2 edge triples child out of range'] or T['L2 edge triples far node with no block']]
    r['notes'] = notes + (['edge triples by tile: ' + '; '.join(tiles)] if tiles else []) + r['notes'] + [
        'pass at every level-2 edge triple naming the far node\'s own (cell, level-1 child)']
    r['passed'] = r['passed'] and not bad
    return r


def judge(graph, shapes, drawn, walked, nodes, count=300, seed=1):
    """Route over the graph and look every link up in the tables the functions above make.

    Returns card_check's result dict. Its notes list every miss once, where a route first met it."""
    st, misses, left = collections.Counter(), {}, {}
    classes = {level: {key[2] for key in at} for level, at in nodes.items()}
    meeting = {level: collections.defaultdict(set) for level in nodes}
    for level, at in nodes.items():
        for key, places in at.items():
            for p in places:
                meeting[level][p].add(key)
    seen = {1: set(), 2: set(), 4: set(), 5: set()}
    # a parcel whose record did not load has links nothing can be said of
    parcels = {1: {key[0] for key in shapes}, **{level: {key[:2] for key in at} for level, at in nodes.items()}}

    def miss(level, key):
        """What the unit would not find of one upper link, or None."""
        if key not in nodes[level]:
            return 'named from level 1 but not in the graph'
        if key not in drawn[level]:
            return 'no shape'
        if drawn[level][key]:
            return drawn[level][key]
        if walked[level].get(key, 'walked') not in ('walked', 'walk leaves the loaded tiles'):
            return 'does not expand: ' + walked[level][key]

    def use(level, key, place):
        if key[:2] in parcels[level] and key not in seen[level]:
            seen[level].add(key)
            what = miss(level, key)
            if what:
                misses[level, key] = (what, place)

    for start, end, path in graph.routes(count, seed):
        st['routes'] += 1
        st['km'] += km(graph.place[start], graph.place[end])
        on = {}             # level: (the last link of the level, the level-1 links since that named none)
        used = set()
        for i, link in path:
            shape = shapes.get(link)
            if link not in seen[1] and link[0] in parcels[1]:
                seen[1].add(link)
                if shape is None or not shape[0]:
                    misses[1, link] = ('no shape' if shape is None else 'shape in the wrong layer', graph.place[i])
            for level in (2, 4, 5):
                key = shape[1].get(level) if shape else None
                if key is None:
                    if level in on and link[1] in classes[level]:
                        on[level][1].append((i, link))
                    else:
                        on.pop(level, None)
                    continue
                last, gap = on.get(level, (key, []))
                if gap and last != key and not nodes[level].get(last, set()) & nodes[level].get(key, set()):
                    # the level's own links from the one to the other stand in for the level-1 links that name none
                    through = {z for p in nodes[level].get(last, ()) for z in meeting[level][p]
                               if z not in (last, key) and nodes[level][z] & nodes[level].get(key, set())}
                    for z in sorted(through):
                        use(level, z, graph.place[gap[0][0]])
                    if not through and len(gap) <= JUNCTION and last[:2] in parcels[level] and key[:2] in parcels[level]:
                        left.setdefault((level, gap[0][1]), (graph.place[gap[0][0]], len(gap)))
                on[level] = (key, [])
                used.add(level)
                use(level, key, graph.place[i])
        for level in (2, 4, 5):
            st['L%d routes with no link' % level] += bool(nodes[level]) and level not in used
    st['km'] = round(st['km'])
    notes = ['level %d link class %d id %d in parcel %s at %.4f,%.4f: %s' % (level, key[-2], key[-1], key[:-2] if level > 1 else key[0], *place, what)
             for (level, key), (what, place) in sorted(misses.items(), key=lambda kv: (kv[0][0], kv[1][1]))]
    notes += ['level %d: a route falls off it at %.4f,%.4f for %d level-1 links of its classes (class %d id %d in parcel %s the first)'
              % (level, *place, n, link[1], link[2], link[0]) for (level, link), (place, n) in sorted(left.items(), key=lambda kv: (kv[0][0], kv[1][0]))]
    ok = True
    for level in (1, 2, 4, 5):
        stuck = sum(k[0] == level and v[0].startswith('does not expand') for k, v in misses.items()) + sum(k[0] == level for k in left)
        st['L%d links driven' % level] = len(seen[level])
        st['L%d not drawn' % level] = sum(k[0] == level for k in misses) + sum(k[0] == level for k in left) - stuck
        ok = ok and not st['L%d not drawn' % level]
        if level > 1:
            st['L%d not expanding' % level] = stuck
            ok = ok and not stuck and st['L%d routes with no link' % level] <= OFF_LEVEL * st['routes']
    notes.append('pass at every driven link drawn and expanding, and %d%% of the routes using no link of a level' % (100 * OFF_LEVEL))
    return card_check.result('drives', ok, st, notes)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--card')
    ap.add_argument('--tiles', nargs='*', default=[])
    ap.add_argument('--lev')
    ap.add_argument('--routes', type=int, default=300)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    if args.card:
        files = card_check.extract_card(args.card)
        names = [k for k in sorted(files) if k.upper().startswith('MAPAL001/') and not k.upper().endswith('LEV3456R.DAT')]
        tiles = [files[k] for k in names]
        lev = next((v for k, v in files.items() if k.upper().endswith('LEV3456R.DAT')), None)
    else:
        tiles, lev, names = args.tiles, args.lev, None
    r = drive(Card(tiles, lev, names), args.routes, args.seed)
    card_check.print_report(os.path.basename(args.card or 'tiles'), [r])
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    return 0 if r['passed'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
