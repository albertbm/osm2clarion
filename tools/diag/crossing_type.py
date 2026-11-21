#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand port of the QY8 unit's crossing classifier, run on a roads.json.

Navi.exe (G214) FUN_005e75c8 takes a table of the links at a node, row 0 the link the route
arrives on, row 1 the link it leaves on, the rest the other arms, and returns the crossing
type: 0 no manoeuvre, 0x13 a branch among roads, 0x14 leaving onto a link that is not a road
code (a slip), other values the motorway types. Each row has a heading in tenths of a degree,
the class, a rank from the class, a closed flag and the link code from shape word 3.

What is ported: the path for ranks 5 and up (classes 3 to 9), which is all of Iceland, with
helpers 005eb378, 005ec790, 005eb3e0, 005eb3a0, 005ea628, 005ebc98, 005eb72c, 005eb8d8,
005eb52c, 005e59f4 and the code-7 part of 005e3c4c.

What is not, and how a node that needs it is reported:
  - the motorway rules (rank under 5) and 005ec80c                       -> 'unread'
  - every rule that tests link code 1, and 005ebf98 which pairs code-1 rows -> 'unread'
  - 005eba88's side test (its last arguments did not decompile): left out, the rest kept
  - 005eb17c's sector numbers come from an unrecovered jump table: taken in angle order
Assumed, not read: the entry heading is the travel direction into the node; an arm traffic
cannot enter is the closed one; settings 0 and 4 are 1000 and 0; shape word 3's other fields
are 0, as on our cards; headings come from OSM points, where the unit uses the first segment
of the drawn shape in parcel units.

    crossing_type.py roads.json --at lat,lon [--codes nocode3|code3]    every route through a node
    crossing_type.py roads.json --slips                                 census of slip forks
"""
import collections
import json
import math
import sys

UNREAD = 'unread'
CLASS = {'motorway': 1, 'trunk': 3, 'primary': 4, 'secondary': 5, 'tertiary': 7,
         'unclassified': 8, 'residential': 8, 'living_street': 8, 'service': 9, 'ferry': 10}
RINGS = ('roundabout', 'circular')
Row = collections.namedtuple('Row', 'h cls rank closed code way')


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


def sides(rows, t, lo, hi, ref, open_only):
    """005ea628 and 005ebc98: which side of the reference row other arms of rank lo..hi lie on, within t."""
    left = right = False
    for a in rows[2:]:
        if open_only and a.closed: continue
        if not lo <= a.rank <= hi: continue
        d = sdiff(rows[ref].h, a.h)
        if ref == 1 and d == 0: continue
        if -t <= d <= 0: left = True
        if 0 <= d <= t: right = True
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


def e59f4(rows, dev):
    """005e59f4 with shape word 3's field at 0: never 1."""
    return 0


def eb72c(rows):
    e, x = rows[0], rows[1]
    arms = [a for a in rows[2:] if not a.closed]
    if road(e.code):
        if not road(x.code): return 0x14 if x.code != 6 else UNREAD
        return 0x13 if any(road(a.code) for a in arms) else 0
    if e.code != 3 or x.code != 3 or any(a.code != 3 for a in arms): return 0x13
    return 0 if eba88(rows) == 1 else 0x13


def classify(rows):
    """005e75c8, the path for ranks of 5 and up."""
    n = len(rows); e, x = rows[0], rows[1]
    if any(r.code == 1 for r in rows) or e.rank < 5 or x.rank < 5: return UNREAD
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
        if in_band(e, x, 1200, 2400) != 0: return 0x13
        return 0x13 if sides(rows, 1350, 0, 15, 0, True) != 0 else 0
    if n == 3:
        a = rows[2]
        if e.code == 0 and dev < 200 and x.code == 3 and a.code == 3 and e.cls == x.cls == a.cls: return 0
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
                if e.code != 0: return 0 if dev < nearest(rows, 0) else 0x13
                return 0x13
            if e.rank != x.rank: return eb72c(rows)
            if eba88(rows) != 1: return 0x13
            if e.code != 0: return 0
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
        return 0 if e59f4(rows, dev) == 1 else 0x13
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


def arms(use, node):
    """Each link end at the node: (way, heading out of the node, may leave by it, may arrive by it)."""
    out = []
    for w, i in use[node]:
        for s in (1, -1):
            j = i + s
            if 0 <= j < len(w['nodes']):
                d = w['dir'] * s
                out.append((w, heading(node, w['nodes'][j]), d >= 0, d <= 0))
    return out


def routes(use, node, codes):
    """(entry way, exit way, crossing type, rows) for every way through the node."""
    A = arms(use, node)
    if len(A) < 3: return
    for i, (we, he, _, arrive) in enumerate(A):
        if not arrive: continue
        for j, (wx, hx, leave, _) in enumerate(A):
            if i == j or not leave: continue
            def row(k, h, closed):
                w = A[k][0]; c = link_class(w)
                return Row(h, c, rank(c), closed, link_code(w, codes), w)
            rows = [row(i, (he + 1800) % 3600, False), row(j, hx, False)]
            rows += [row(k, A[k][1], not A[k][2]) for k in range(len(A)) if k not in (i, j)]
            yield we, wx, classify(rows), rows


def show(t):
    return t if t == UNREAD else {0: 'none', 0x13: 'branch 0x13', 0x14: 'slip 0x14'}.get(t, hex(t))


def main(argv):
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
            A = arms(use, node)
            slips = [a for a in A if a[0]['hw'].endswith('_link') and a[0]['dir'] != 0 and a[2]]
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
