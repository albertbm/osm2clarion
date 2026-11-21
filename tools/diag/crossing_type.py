#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand port of the QY8 unit's crossing classifier, run on a roads.json.

Navi.exe (G214) FUN_005e75c8 takes a table of the links at a node, row 0 the link the route
arrives on, row 1 the link it leaves on, the rest the other arms, and returns the crossing
type: 0 no manoeuvre, 0x13 a branch among roads, 0x14 leaving onto a link that is not a road
code (a slip), other values the motorway types. Each row has a heading in tenths of a degree,
the class, a rank from the class, a closed flag and the link code from shape word 3.

With --tiles the links come from the card: level-1 shapes, an arm per segment leaving a link
end, the heading that first segment's, class and code from shape words 2 and 3. A shape whose
word 3 has bits 11-9 or 5-4 set is reported 'unread': the ported rules take them as 0.

What is ported: the path for ranks 5 and up (classes 3 to 9), which is all of Iceland, with
helpers 005eb378, 005ec790, 005eb3e0, 005eb3a0, 005ea628, 005ebc98, 005eb72c, 005eb8d8,
005eb52c, 005e59f4 and the code-7 part of 005e3c4c.

What is not, and how a node that needs it is reported:
  - the motorway rules (rank under 5) and 005ec80c                       -> 'unread'
  - 005ebf98, which pairs the code-1 rows of a divided road and can turn their headings:
    read, and a node where it would turn a pair is 'unread'; the code-1 rules of 005e75c8
    for three links are ported
  - 005eba88's side test (its last arguments did not decompile): left out, the rest kept
  - 005eb17c's sector numbers come from an unrecovered jump table: taken in angle order
Assumed, not read: the entry heading is the travel direction into the node; an arm traffic
cannot enter is the closed one; settings 0 and 4 are 1000 and 0; shape word 3's other fields
are 0, as on our cards; headings come from OSM points, where the unit uses the first segment
of the drawn shape in parcel units.

    crossing_type.py roads.json --at lat,lon [--codes nocode3|code3]    every route through a node
    crossing_type.py roads.json --slips                                 census of slip forks
    crossing_type.py --tiles OLD.DAT[,..] [NEW.DAT[,..]] --at lat,lon | --slips     the same on tiles
"""
import collections
import json
import math
import sys

UNREAD = 'unread'
CLASS = {'motorway': 1, 'trunk': 3, 'primary': 4, 'secondary': 5, 'tertiary': 7,
         'unclassified': 8, 'residential': 8, 'living_street': 8, 'service': 9, 'ferry': 10}
RINGS = ('roundabout', 'circular')
Row = collections.namedtuple('Row', 'h cls rank closed code way plain f8')
Arm = collections.namedtuple('Arm', 'key h leave arrive cls code plain way f8')


def road(code):
    """005eb378: the codes that count as a road."""
    return code in (0, 1, 4, 5)


def rank(cls):
    """005ec790."""
    return 0 if cls < 3 else 5 if cls < 8 else 11 if cls < 10 else 15


def adiff(a, b):
    d = abs(a - b) % 3600
    return 3600 - d if d > 1800 else d


def sdiff(a, b):
    d = (a - b) % 3600
    return d - 3600 if d > 1800 else d


def within(a, b, t):
    """005eb3e0 on two rows: 0 when b is inside t of a, else 2."""
    return 0 if -t < sdiff(a.h, b.h) < t else 2


def rel(e, x):
    """The exit's heading seen from the reversed entry; 1800 is straight on (005eb3a0, 005eb17c)."""
    return (x.h - (e.h + 1800)) % 3600


def in_band(e, x, lo, hi):
    """005eb3a0: 0 inside the band."""
    return 0 if lo <= rel(e, x) <= hi else 0xffff


def sector(e, x):
    """005eb17c. The bands are read; their numbers are assumed to run in angle order."""
    v = rel(e, x)
    for k, top in enumerate((100, 200, 1350, 1700, 1900, 2250, 3400, 3500)):
        if v <= top: return k
    return 8


def sides(rows, t, lo, hi, ref, open_only, side=0):
    """005ea628 and 005ebc98: which side of the reference row other arms of rank lo..hi lie on, within t."""
    left = right = False
    for a in rows[2:]:
        if open_only and a.closed: continue
        if not lo <= a.rank <= hi: continue
        d = sdiff(rows[ref].h, a.h)
        if ref == 1 and d == 0: continue
        if side in (0, 2) and -t <= d <= 0: left = True
        if side in (0, 1) and 0 <= d <= t: right = True
    return (2 if left else 0) | (1 if right else 0)


def nearest(rows, ref):
    best = 3600
    for a in rows[2:]:
        if not a.closed and a.rank < 14: best = min(best, adiff(a.h, rows[ref].h))
    return best


def eba88(rows):
    """005eba88 without its side test: the route is straighter than every other arm."""
    return 1 if adiff(rows[1].h, rows[0].h) < nearest(rows, 0) else 0


def eb8d8(rows):
    e, x = rows[0], rows[1]
    if not road(x.code) or within(e, x, 470): return 0
    if not any(within(e, a, 470) == 0 and (road(a.code) or a.code == 3) for a in rows[2:]): return 1
    if not any(within(e, a, 470) == 0 and not a.closed and a.rank <= x.rank for a in rows[2:]): return 1
    return eba88(rows)


def eb52c(rows):
    e, x = rows[0], rows[1]
    best, arm = 3600, None
    for a in rows[2:]:
        if not a.closed and a.rank < 14 and adiff(a.h, e.h) < best: best, arm = adiff(a.h, e.h), a
    if arm is not None and best <= adiff(x.h, e.h) and not (e.code == 1 and x.code == 1 and arm.code == 0):
        return 2
    return 0


def eb72c(rows):
    e, x = rows[0], rows[1]
    arms = [a for a in rows[2:] if not a.closed]
    if road(e.code):
        if not road(x.code): return 0x14 if x.code != 6 else UNREAD
        return 0x13 if any(road(a.code) for a in arms) else 0
    if e.code != 3 or x.code != 3 or any(a.code != 3 for a in arms): return 0x13
    return 0 if eba88(rows) == 1 else 0x13


def pairs_rewritten(rows):
    """005ebf98 as far as it is read: it pairs an arriving code-1 row with the code-1 row that
    carries it on, and turns both to their mean heading when a plain road of the same rank
    leaves about opposite to that mean. True when it would turn a pair; the turning itself is
    not ported, so such a node is unread. The exact-heading matches it also tests are left out."""
    ones_in = [i for i, r in enumerate(rows) if r.code == 1 and 5 <= r.rank < 14 and (i == 0 or r.closed)]
    ones_out = [i for i, r in enumerate(rows) if r.code == 1 and 5 <= r.rank < 14 and i != 0 and not r.closed]
    if len(rows) < 3 or len(ones_in) != len(ones_out) or not ones_in or len(ones_in) > 4: return False
    for a in ones_in:
        out_a = (rows[a].h + 1800) % 3600 if a == 0 else rows[a].h
        best = None
        for b in ones_out:
            if rows[b].cls != rows[a].cls: continue
            d = sdiff(out_a, rows[b].h)
            if d > 0: continue                                   # setting 4 taken as 0
            if not 2300 <= (rows[b].h - (out_a + 1800)) % 3600 <= 3600: continue
            if best is None or abs(d) > best[0]: best = (abs(d), b)
        if best is None: continue
        b = best[1]
        through = (out_a + 1800) % 3600
        mean = (rows[b].h + sdiff(through, rows[b].h) // 2) % 3600
        for i, r in enumerate(rows):
            if i in (a, b) or r.closed or r.code != 0 or r.rank != rows[a].rank: continue
            h = (r.h + 1800) % 3600 if i == 0 else r.h
            if 1580 <= (h - mean) % 3600 <= 2020: return True
    return False


def classify(rows):
    """005e75c8, the path for ranks of 5 and up."""
    n = len(rows); e, x = rows[0], rows[1]
    if any(not r.plain for r in rows) or e.rank < 5 or x.rank < 5: return UNREAD
    if any(r.code == 1 for r in rows) and pairs_rewritten(rows): return UNREAD
    dev = adiff(x.h, e.h)
    opened = [a for a in rows[2:] if not a.closed]
    near90 = sum(1 for a in opened if adiff(a.h, e.h) < 900)
    first_side = -1
    for a in opened:
        first_side = a.code
        if not road(a.code): break
    if sides(rows, 1800, 0, 14, 0, False) == 0: return 0
    on_ring, off_ring = e.code == 7, x.code == 7
    if on_ring != off_ring and e.cls != 12: return 0x13
    if sides(rows, 1800, 0, 15, 0, False) == 0:
        return 0x13 if within(e, x, 450) else 0
    if within(e, x, 470) == 2:
        if n == 3 and e.rank == x.rank == rows[2].rank and rows[2].code == 1:
            a = rows[2]                                          # leaving or joining a divided road; setting 4 taken as 0
            if not a.closed and e.code == 1 and x.code == 0:
                if in_band(e, x, 1200, 1800) == 0 and in_band(e, a, 2700, 3600) == 0: return 0
            if a.closed and e.code == 0 and x.code == 1:
                if in_band(e, x, 1200, 1800) == 0 and sides(rows, 600, 0, 15, 1, False, 2) == 2: return 0
        if in_band(e, x, 1200, 2400) != 0: return 0x13
        return 0x13 if sides(rows, 1350, 0, 15, 0, True) != 0 else 0
    if n == 3:
        a = rows[2]
        if e.code in (0, 1) and dev < 200 and x.code == 3 and a.code == 3 and e.cls == x.cls == a.cls: return 0
    go_8770 = False
    if sides(rows, 300, 0, 15, 1, True) == 0:
        if sides(rows, 470, 0, 15, 1, True) != 0:
            go_8770 = True
        else:
            if n == 3 or not opened:
                best, cls = 3600, None
                for a in rows[2:]:
                    if adiff(x.h, a.h) < best: best, cls = adiff(x.h, a.h), a.cls
                if dev < 351 and best > 899 and sides(rows, 800, 0, 15, 0, True) == 0 and not on_ring:
                    if e.cls == x.cls == cls: return 0
                    if n > 2 and rows[2].closed: return 0
            if within(e, x, 250) == 0:
                if x.code != 3: return 0
                if e.code not in (0, 1): return 0 if dev < nearest(rows, 0) else 0x13
                return 0x13
            if e.rank != x.rank: return eb72c(rows)
            if eba88(rows) != 1: return 0x13
            if e.code not in (0, 1): return 0
            return 0x13 if x.code == 3 else 0
    else:
        sec = sector(e, x)
        if sec in (3, 5) and any(sector(e, a) == sec for a in rows[2:]) and sides(rows, 470, 0, x.rank, 0, True) != 0:
            return eb72c(rows)
        go_8770 = True
    assert go_8770
    if sides(rows, 1800, 0, x.rank, 0, True) == 0:
        if x.rank < e.rank: return eb72c(rows)
        if road(e.code) and x.code == 3: return 0x13
        if eb52c(rows) != 2: return 0
        if n > 3 and any(r.f8 for r in rows): return UNREAD   # 005e59f4 reads that field; not ported
        return 0x13   # 005e59f4 is never 1 while the field is 0
    route_near = within(e, x, 210) == 0
    arm_near = any(not a.closed and within(e, a, 210) == 0 for a in rows[2:])
    if route_near and arm_near:
        best = 3600
        for a in rows[2:]:
            if e.rank <= a.rank < 14 and not a.closed: best = min(best, adiff(a.h, e.h))
        if best <= dev: return 0x13
        if not road(e.code) or not road(x.code) or near90 != 1: return 0x13
        return 0 if first_side == 3 else 0x13
    if eb8d8(rows) != 1 or e.rank < x.rank: return eb72c(rows)
    return 0x13 if eb52c(rows) == 2 else 0


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110540)


def heading(a, b):
    return round(10 * math.degrees(math.atan2((b[0] - a[0]) * math.cos(math.radians(a[1])), b[1] - a[1]))) % 3600


def link_class(w):
    hw = w['hw'][:-5] if w['hw'].endswith('_link') else w['hw']
    if hw == 'secondary' and not w.get('ref'): return 6
    return CLASS.get(hw, 8)


def link_code(w, codes):
    if w.get('junction') in RINGS: return 7
    if codes == 'code3' and w['hw'].endswith('_link') and w['dir'] != 0: return 3
    return 0


def load(path):
    use = collections.defaultdict(list)
    for w in json.load(open(path)):
        for i, n in enumerate(w['nodes']): use[tuple(n)].append((w, i))
    return use


def arms(use, node, codes='nocode3'):
    """Each link end at the node, from OSM."""
    out = []
    for w, i in use[node]:
        for s in (1, -1):
            j = i + s
            if 0 <= j < len(w['nodes']):
                d = w['dir'] * s
                out.append(Arm((w['id'], i, s), heading(node, w['nodes'][j]), d >= 0, d <= 0,
                               link_class(w), link_code(w, codes), True, w, 0))
    return out


def arm_routes(A):
    """(entry arm, exit arm, crossing type, rows) for every way through a node with arms A."""
    if len(A) < 3: return
    for i, ae in enumerate(A):
        if not ae.arrive: continue
        for j, ax in enumerate(A):
            if i == j or not ax.leave: continue
            def row(a, h, closed): return Row(h, a.cls, rank(a.cls), closed, a.code, a.way, a.plain, a.f8)
            rows = [row(ae, (ae.h + 1800) % 3600, False), row(ax, ax.h, False)]
            rows += [row(a, a.h, not a.leave) for k, a in enumerate(A) if k not in (i, j)]
            yield ae, ax, classify(rows), rows


def routes(use, node, codes):
    for ae, ax, t, rows in arm_routes(arms(use, node, codes)): yield ae.way, ax.way, t, rows


def tile_nodes(path):
    """{(row, col, x, y): [Arm]} of a tile's level-1 road shapes: an arm per shape segment
    that leaves a link end, its heading that first segment's, as the unit takes it."""
    import os, struct
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    from tile_scan import TileScan
    import clarion_bg, fa_encode, rd_check
    nodes = collections.defaultdict(list)
    with TileScan(path) as t:
        recs = {k: r.plain() for k, r in rd_check.shape_records(t, 1).items()}
    for code, pl in recs.items():
        try: shapes = fa_encode.parse_record(pl, 1)[2]
        except Exception: continue
        if not all(S['length'] for S in shapes): continue
        w = struct.unpack('>%dH' % (len(pl) // 2), pl)
        try: row, col = clarion_bg.parcel_rowcol(1, *code)
        except AssertionError: continue   # not a level-1 parcel code
        kx = math.cos(math.radians(row / 48)) / 32; ky = 1 / 48
        for S in shapes:
            if not S['ok']: continue
            at = S['at']; w3 = w[at + 3]
            c = w3 >> 12; c = 7 if c == 8 else c
            plain = (w3 & 0xe30) == 0          # word 3 bits 11-9 and 5-4, which the ported rules take as 0
            f8 = (w3 >> 6) & 7                 # bits 8-6, read by 005e59f4 only
            idx = list(S['points'])
            P = [(w[i] & 0x7ff, w[i + 1] & 0x7ff, w[i + 1] & 0xc000, S['points'][i][0]) for i in idx]
            for k, (x, y, _, role) in enumerate(P):
                if role == 'vertex': continue
                for s in (1, -1):
                    j = k + s
                    if not 0 <= j < len(P): continue
                    bits = P[max(k, j)][2]        # a segment's one-way bits sit on its later point
                    fwd = bits == 0x4000; back = bits == 0x8000
                    leave = not (back if s == 1 else fwd); arrive = not (fwd if s == 1 else back)
                    h = round(10 * math.degrees(math.atan2((P[j][0] - x) * kx, (P[j][1] - y) * ky))) % 3600
                    nodes[(row, col, x, y)].append(Arm((at, k, s), h, leave, arrive, S['cls'], c, plain, None, f8))
    return nodes


def node_lonlat(key):
    row, col, x, y = key
    return (col + x / 2048) / 32 - 924, (row + y / 2048) / 48


def show(t):
    return t if t == UNREAD else {0: 'none', 0x13: 'branch 0x13', 0x14: 'slip 0x14'}.get(t, hex(t))


def kind_of(rows):
    return 'takes the slip' if rows[1].code == 3 else 'from a slip' if rows[0].code == 3 else 'passes the slip'


def tiles_main(argv):
    """--tiles OLD[,OLD..] [NEW[,NEW..]] with --at lat,lon or --slips: the same runs on tiles."""
    i = argv.index('--tiles')
    sets = [a.split(',') for a in argv[i + 1:i + 3] if not a.startswith('--')]
    loaded = []
    for paths in sets:
        nodes = {}
        for p in paths: nodes.update(tile_nodes(p))
        loaded.append(nodes)
    if '--at' in argv:
        lat, lon = (float(v) for v in argv[argv.index('--at') + 1].split(','))
        for n, nodes in enumerate(loaded):
            key = min((k for k in nodes if len(nodes[k]) >= 3), key=lambda k: dist_m(node_lonlat(k), (lon, lat)))
            print('set %d node %.6f,%.6f (%.0f m off)' % ((n + 1,) + node_lonlat(key)[::-1] + (dist_m(node_lonlat(key), (lon, lat)),)))
            for ae, ax, t, rows in arm_routes(nodes[key]):
                print('   class %d -> class %d  dev %5.1f  codes %s  %s' % (
                    ae.cls, ax.cls, adiff(rows[1].h, rows[0].h) / 10, [r.code for r in rows], show(t)))
        return 0
    new = loaded[-1]; old = loaded[0] if len(loaded) == 2 else None
    tally = collections.Counter(); lost = []; dropped = []
    for key, A in new.items():
        if len(A) < 3 or not any(a.code == 3 and a.leave for a in A): continue
        was = {(ae.key, ax.key): t for ae, ax, t, _ in arm_routes(old[key])} if old else {}
        for ae, ax, t, rows in arm_routes(A):
            kind = kind_of(rows)
            if old is None:
                tally[(kind, 'codes in,out %d,%d' % (rows[0].code, rows[1].code), show(t))] += 1; continue
            o = was[(ae.key, ax.key)]
            tally[(kind, show(o), show(t))] += 1
            if o not in (0, UNREAD) and t == 0: (lost if kind == 'takes the slip' else dropped).append((key, rows))
    for k, v in sorted(tally.items()): print('%-16s %-22s %-12s %6d' % (k + (v,)))
    if old is not None:
        print('calls dropped:', len(dropped), ' routes that take a slip and lose their call:', len(lost))
        for key, rows in lost[:20]:
            print('   %.5f,%.5f dev %.1f codes %s' % (node_lonlat(key)[::-1] + (adiff(rows[1].h, rows[0].h) / 10, [r.code for r in rows])))
    return 0


def main(argv):
    if '--tiles' in argv: return tiles_main(argv)
    use = load(argv[1])
    codes = argv[argv.index('--codes') + 1] if '--codes' in argv else None
    if '--at' in argv:
        lat, lon = (float(v) for v in argv[argv.index('--at') + 1].split(','))
        node = min(use, key=lambda n: dist_m(n, (lon, lat)))
        print('node %.6f,%.6f' % (node[1], node[0]))
        for cs in ([codes] if codes else ['nocode3', 'code3']):
            for we, wx, t, rows in routes(use, node, cs):
                print('  %s  %-14s %-18s -> %-14s %-18s dev %5.1f  codes %s  %s' % (
                    cs, we['hw'], (we.get('name') or '')[:18], wx['hw'], (wx.get('name') or '')[:18],
                    adiff(rows[1].h, rows[0].h) / 10, [r.code for r in rows], show(t)))
        return 0
    if '--slips' in argv:
        tally = collections.Counter(); lost = []
        for node in use:
            A = arms(use, node, 'code3')
            slips = [a for a in A if a.code == 3 and a.leave]
            if len(A) < 3 or not slips: continue
            old = {(id(we), id(wx)): t for we, wx, t, _ in routes(use, node, 'nocode3')}
            for we, wx, t, rows in routes(use, node, 'code3'):
                kind = 'takes the slip' if rows[1].code == 3 else 'from a slip' if rows[0].code == 3 else 'passes the slip'
                o = old[(id(we), id(wx))]
                tally[(kind, show(o), show(t))] += 1
                if kind == 'takes the slip' and o not in (0, UNREAD) and t == 0:
                    lost.append((node, we, wx, rows))
        for k, v in sorted(tally.items()): print('%-16s no code 3 %-12s code 3 %-12s %5d' % (k + (v,)))
        print('routes that take a slip and lose their call:', len(lost))
        for node, we, wx, rows in lost[:20]:
            print('   %.5f,%.5f %s -> %s dev %.1f codes %s' % (node[1], node[0], we.get('name'), wx.get('name'),
                                                               adiff(rows[1].h, rows[0].h) / 10, [r.code for r in rows]))
        return 0
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
