#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check section-2 road records against what their own bodies say the header must hold.

    ./rd_check.py [--country 352] [--card IMAGE] TILE...     TILE is a path, or with --card
                                                             a name such as B60R080R.DAT
    -v                                                       print every failing record
    --graph [SHAPES.json ...]                                connected pieces of the level-1
                                                             graph, named from shape indexes,
                                                             and edge entries left unanswered

The header rule. Measured 18 Oct 2026 on 14 factory tiles off 214-062, 214-066 and 218-120,
both levels, multi-class and border records included: 183,567 of 183,567 records whose body
parses (115 in two dense city tiles do not yet):

- words 2-5 are zero, and the units end where part a does;
- from word 26 to the start of unit 0 sit the record's ISO 3166 numeric country codes, one
  word each: one country and unit 0 at word 27, or two where a border crosses the parcel;
- unit 0 words 0-3 are u32 nodes and u32 link ends, then eleven (nodes, link ends) pairs
  at u4..u25, one per class group (slot). A node counts in the group of its own class, the
  top nibble of its y word; its link ends are its entries, parcel-edge connections
  excluded, and count in the same group. The totals in words 0-3 are the sums, so edge
  connections are never counted.

Two body rules ride along, since a writer has to get them right for the header to mean
anything: a node's class is the lowest link class among its entries (the class word's low
nibble), and an entry's high byte is the class of the node at the link's far end.
"""
import collections, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_rd
from tile_scan import TileScan


def slot(cls):
    """Pair index in u4..u25 for a node class: 0-2 at u6, 3-7 at u8, 8 at u10, 9 at u12, 10
    and up (ferries) at u24. u4/u5 and u14..u23 were zero on every factory record read."""
    return 1 if cls <= 2 else 2 if cls <= 7 else 3 if cls == 8 else 4 if cls == 9 else 10

SLOTS = 11


def real_entries(node):
    return [e for e in node['entries'] if not e['cls'] & 0x8000]


def expected(rec):
    """(nodes, link ends, [(nodes, link ends)] * 11) as the body of a parsed record implies."""
    pairs = [[0, 0] for _ in range(SLOTS)]
    for nd in rec['nodes']:
        ents = real_entries(nd)
        if not ents: continue
        g = slot(nd['ncls'])
        pairs[g][0] += 1; pairs[g][1] += len(ents)
    return sum(p[0] for p in pairs), sum(p[1] for p in pairs), [tuple(p) for p in pairs]


def part_a(plain, level):
    """The graph part of a record: level-1 records are one zlib stream, level-2 records ship
    raw as part a then part b, and part a ends where its last unit does."""
    if level == 1: return plain
    units = [struct.unpack_from('>II', plain, 12 + 8 * i) for i in range(5)]
    return plain[:2 * max(o + n for o, n in units if o != 0xffffffff)]


def check(plain, level=1, country=None):
    """(header problems, body problems) for one inflated record; both empty when it passes."""
    pa = part_a(plain, level)
    w = struct.unpack('>%dH' % (len(pa) // 2), pa[:len(pa) // 2 * 2])
    hp, bp = [], []
    if w[2:6] != (0, 0, 0, 0): hp.append('words 2-5 %r, want zero' % (w[2:6],))
    rec = clarion_rd.parse_record(pa)
    if 'nnodes' not in rec: return hp + ['no unit 0'], bp
    if country is not None and list(rec['countries']) != [country]:
        hp.append('countries %r, want [%d]' % (list(rec['countries']), country))
    ends = [o + n for o, n in rec['pairs'] if o != 0xffffffff]
    if not 27 <= rec['pairs'][0][0] <= 30 or max(ends) != len(w):
        hp.append('unit 0 at %r, units end at %d of %d words' % (rec['pairs'][0], max(ends), len(w)))
    if rec.get('error') or rec['consumed'] != rec['unit_len']:
        return hp, ['body does not parse: %s' % (rec.get('error') or 'stops at %d of %d' % (rec['consumed'], rec['unit_len']))]
    u = rec['units'][0]
    nn, nl, pairs = expected(rec)
    if (rec['nnodes'], rec['nlinks']) != (nn, nl):
        hp.append('totals (%d, %d), body gives (%d, %d)' % (rec['nnodes'], rec['nlinks'], nn, nl))
    if rec['classes'] != pairs:
        hp.append('pairs %s, body gives %s' % (fmt(rec['classes']), fmt(pairs)))
    nodes = rec['nodes']
    for i, nd in enumerate(nodes):
        ents = real_entries(nd)
        if not ents: continue
        low = min(e['cls'] & 0xf for e in ents)
        if nd['ncls'] != low: bp.append('node %d class %d, its links say %d' % (i, nd['ncls'], low))
        for e in ents:
            if e['other'] & 0x4000: continue      # a node in another record, seen on factory cards
            if e['other'] >= len(nodes): bp.append('node %d entry points at node %d of %d' % (i, e['other'], len(nodes))); continue
            far = nodes[e['other']]['ncls']
            if (e['cls'] >> 8) & 0xf != far:
                bp.append('node %d entry class %04x, far node class %d' % (i, e['cls'], far))
    return hp, bp


SIDE = {0x8000: (1, 0), 0xa000: (0, 1), 0xc000: (-1, 0), 0xe000: (0, -1)}   # top, right, bottom, left


def rowcol(level, w0, w1):
    """Absolute parcel (row, col) from a level-1 or level-2 parcel code (clarion_bg.parcel_code)."""
    if level == 1:
        return (((w0 >> 8) & 0x7f) << 6 | ((w0 >> 4) & 0xf) << 2 | (w0 & 3),
                (w1 >> 8) << 6 | ((w1 >> 4) & 0xf) << 2 | (w1 & 3))
    return ((w0 >> 8) & 0x7f) << 4 | (w0 >> 4) & 0xf, (w1 >> 8) << 4 | (w1 >> 4) & 0xf


def components(tiles, level=1):
    """Connected pieces of one level's road graph over whole tiles (paths or bytes)."""
    def plains():
        for tile in tiles:
            with TileScan(tile) as t:
                for r in t.records(section=2, level=level):
                    yield r.plain()
    return pieces(plains(), level)


def pieces(plains, level=1):
    """Connected pieces of a set of inflated records, joined across parcel edges through the
    edge entries: [(set of (row, col, link id), node count)], largest first."""
    parent = {}
    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    def union(a, b): parent[find(a)] = find(b)
    links = {}
    for plain in plains:
        rec = clarion_rd.parse_record(part_a(plain, level))
        if 'nnodes' not in rec: continue
        rc = rowcol(level, *rec['code'])
        for i, nd in enumerate(rec['nodes']):
            me = rc + (i,)
            find(me)
            for e in nd['entries']:
                if e['cls'] & 0x8000:
                    dr, dc = SIDE[e['cls'] & 0xe000]
                    union(me, (rc[0] + dr, rc[1] + dc, e['other'] & 0x3fff))
                else:
                    links.setdefault(rc + (e['id'] & 0x1fff,), me)
                    if not e['other'] & 0x4000: union(me, rc + (e['other'],))
    groups = collections.defaultdict(lambda: [set(), 0])
    for n in list(parent):
        groups[find(n)][1] += 1
    for lk, n in links.items(): groups[find(n)][0].add(lk)
    return sorted(((g, n) for g, n in groups.values()), key=lambda c: -c[1])

OPPOSITE = {0x8000: 0xc000, 0xc000: 0x8000, 0xa000: 0xe000, 0xe000: 0xa000}


def edge_joins(plains, level=1):
    """(edge entries read, [unanswered]) over a set of inflated records. An edge entry
    names a side and the node it meets in the neighbour across it; that node must carry an
    entry on the opposite side naming this one back. Each miss is (row, col, node, side,
    far node, why). Held on five factory tiles off 214-062, about 224,000 level-1 edge
    entries: every miss pointed into a parcel outside the tile."""
    edges, parcels = {}, set()
    for plain in plains:
        rec = clarion_rd.parse_record(part_a(plain, level))
        if 'nnodes' not in rec: continue
        rc = rowcol(level, *rec['code']); parcels.add(rc)
        for i, nd in enumerate(rec['nodes']):
            edges[rc + (i,)] = [(e['cls'] & 0xe000, e['other'] & 0x3fff) for e in nd['entries'] if e['cls'] & 0x8000]
    bad, n = [], 0
    for (r, c, i), ents in edges.items():
        for side, j in ents:
            n += 1
            dr, dc = SIDE[side]
            far = edges.get((r + dr, c + dc, j))
            if (r + dr, c + dc) not in parcels:
                bad.append((r, c, i, side, j, 'neighbour parcel not read'))
            elif far is None:
                bad.append((r, c, i, side, j, 'no such node across the edge'))
            elif (OPPOSITE[side], i) not in far:
                bad.append((r, c, i, side, j, 'far node does not answer: %s' % far))
    return n, bad


def fmt(pairs):
    return ' '.join('u%d:%d/%d' % (4 + 2 * i, n, l) for i, (n, l) in enumerate(pairs) if n or l) or 'empty'


def check_tile(tile, country=None):
    """Tally one tile: path or bytes. Returns {(level, part, ok): n}, the
    countries seen per level, and up to 20 failing examples."""
    tally, countries, bad = collections.Counter(), collections.Counter(), []
    with TileScan(tile) as t:
        for sid, lev, doff, _ in t._dirs(2):
            kind = struct.unpack_from('>H', t.m, doff)[0]
            assert kind == 0x1046, 'section-2 directory kind %#x; only 0x1046 is walked right' % kind
        for r in t.records(section=2):
            plain = r.plain()
            hp, bp = check(plain, r.level, country)
            off = struct.unpack_from('>I', plain, 12)[0]
            countries[(r.level, struct.unpack_from('>%dH' % (off - 26), plain, 52))] += 1
            tally[(r.level, 'header', not hp)] += 1
            tally[(r.level, 'body', not bp)] += 1
            if not bp: tally[(r.level, 'header where the body is sound', not hp)] += 1
            if (hp or bp) and len(bad) < 20: bad.append((r.level, r.slot, r.k, hp[:3], bp[:3]))
    return tally, countries, bad


# A record whose body does not parse, or breaks the body rules, is one this reader cannot
# vouch for; dense factory cities have a few (0.1%, an entry form not decoded yet).
PARTS = ('header', 'body', 'header where the body is sound')


def main(argv):
    country = None; card = None; verbose = '-v' in argv
    if verbose: argv.remove('-v')
    if '--country' in argv:
        i = argv.index('--country'); country = int(argv[i + 1]); del argv[i:i + 2]
    if '--card' in argv:
        i = argv.index('--card'); card = argv[i + 1]; del argv[i:i + 2]
    shapes = []
    if '--graph' in argv:
        i = argv.index('--graph'); del argv[i]
        while i < len(argv) and argv[i].endswith('.json'): shapes.append(argv.pop(i))
        graph = True
    else:
        graph = False
    if not argv: raise SystemExit(__doc__)
    total = collections.Counter(); failed = False
    F = None
    if card:
        import fatread
        F = fatread.Fat32(card)
    for name in argv:
        tile = F.read(name if name.startswith('/') else '/MAPAL001/' + name) if F else name
        tally, countries, bad = check_tile(tile, country)
        total.update(tally)
        line = []
        for lev in (1, 2):
            for part in PARTS:
                ok, no = tally[(lev, part, True)], tally[(lev, part, False)]
                if ok or no: line.append('L%d %s %d/%d' % (lev, part, ok, ok + no))
        cs = ' '.join('L%d:%s x%d' % (l, '+'.join(map(str, c)), n) for (l, c), n in sorted(countries.items()))
        print('%s  %s  country %s' % (os.path.basename(name), ', '.join(line) or 'no records', cs))
        for b in bad if verbose else bad[:3]:
            print('   level %d slot %d col %d: %s %s' % b)
        failed |= any(not ok for (_, _, ok), n in tally.items() if n)
    if len(argv) > 1:
        print('all: ' + ', '.join('L%d %s %d/%d' % (lev, part, total[(lev, part, True)],
                                                     total[(lev, part, True)] + total[(lev, part, False)])
                                    for lev in (1, 2) for part in PARTS
                                    if total[(lev, part, True)] + total[(lev, part, False)]))
    if graph:
        import json
        names = {}
        for path in shapes:
            for R in json.load(open(path))['records']:
                at = rowcol(1, *R['code'])
                for S in R['shapes']:
                    for l in S['links']: names[at + (l['id'],)] = S['name']
        tiles = [F.read(n if n.startswith('/') else '/MAPAL001/' + n) if F else n for n in argv]
        for k, (lks, nn) in enumerate(components(tiles)):
            named = collections.Counter(names.get(l, '?') or 'unnamed' for l in lks) if names else None
            print('piece %d: %d nodes, %d links%s' % (k, nn, len(lks), (': ' + ', '.join(
                '%s %d' % (n, c) for n, c in named.most_common())) if named else ''))
        for lev in (1, 2):
            def plains():
                for tile in tiles:
                    with TileScan(tile) as t:
                        for r in t.records(section=2, level=lev): yield r.plain()
            n, bad = edge_joins(plains(), lev)
            out = sum(b[5] == 'neighbour parcel not read' for b in bad)
            print('level %d edge entries %d, unanswered %d (%d into parcels not read)' % (lev, n, len(bad), out))
            for b in bad[:5] if not verbose else bad:
                print('   row %d col %d node %d side %#x far node %d: %s' % b)
            failed |= len(bad) > out
    if F: F.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
