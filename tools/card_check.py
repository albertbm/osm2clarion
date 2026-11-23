#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a built card's road graph and coarse routing file against every format rule
measured off factory tiles, and diff a card against the previous one field by field.

    card_check.py --card A.img [--cells C,C...] [--roads roads.json] [--prev B.img] [--factory TILE ...] [--json]
    card_check.py --tiles TILE ... [--lev LEV3456R.DAT] [--roads roads.json] [--factory TILE ...] [--json]

With --card, MAPAL001/*.DAT and LEV3456R.DAT are read straight off the FAT image (no
mount, no full-image load). With --tiles, the files are given directly - useful for a
quick check against a factory tile with no card to extract from.

Each rule below started as a one-off measurement script, checked against factory
tiles and a built card before being folded in here. A rule that fails on factory
data is a rule bug, not a card bug - run --factory and
read the factory column before trusting a FAIL on the card under test.

--roads names the roads.json the tiles were built from. Level 1 is then held against the
OSM ways too, so a road OSM has whole and the card draws in pieces fails at any level.

--cells names the road cells the card must carry; a missing one, one with no L1 or L2
records, or any record that does not parse is a FAIL.

Exit status: 0 if every rule passes on the card under test, 1 otherwise (factory and
--prev results never affect the exit status).
"""
import argparse
import collections
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg
import clarion_format
import clarion_rd
import fa_encode
import lev_file
import rd_check
import rd_encode
from tile_scan import TileScan

SIDE = {0x8000: (1, 0), 0xa000: (0, 1), 0xc000: (-1, 0), 0xe000: (0, -1)}   # top, right, bottom, left
OPPOSITE = {0x8000: 0xc000, 0xc000: 0x8000, 0xa000: 0xe000, 0xe000: 0xa000}
MAXSTEPS = 400


# Loading: tiles and LEV3456R.DAT into the record dicts every rule below reads.

def two_records(fmt, level):
    """QY7 (X08-02) stores a graph record's part A and part B as zlib records of their own, in
    the pairs 2c and 2c + 1 of a column, at levels 2, 4 and 5; QY8 ships one record."""
    return clarion_format.slot_body(2, dict(fmt.levels[2])[level]) == 512


def load_road_tiles(paths, errors=None):
    """L1 {(row,col): parsed record}, L2 {(row,col): (parsed a, parsed part-b)},
    SH1 {(row,col): section-1 0x00fa Rec} over a set of tile files or bytes. Records that do
    not parse are counted in `errors` (a Counter) when one is given."""
    L1, L2, SH1 = {}, {}, {}
    errors = collections.Counter() if errors is None else errors
    for p in paths:
        with TileScan(p) as t:
            for r in t.records(section=2, level=1):
                try:
                    a = clarion_rd.parse_record(rd_check.part_a(r.plain(), 1))
                except Exception:
                    errors['L1'] += 1
                    continue
                if 'nnodes' in a:
                    L1[clarion_bg.parcel_rowcol(1, *a['code'])] = a
            two = two_records(t.fmt, 2)
            parts = {}
            for r in t.records(section=2, level=2):
                pl = r.plain()
                if two:
                    parts.setdefault((r.dir_off, r.slot, r.k // 2), {})[r.k % 2] = pl
                    continue
                try:
                    pa_b = rd_check.part_a(pl, 2)
                    a = clarion_rd.parse_record(pa_b)
                    b = clarion_rd.parse_part_b(pl[len(pa_b):])
                except Exception:
                    errors['L2'] += 1
                    continue
                if 'nnodes' in a:
                    L2[clarion_bg.parcel_rowcol(2, *a['code'])] = (a, b)
            for ab in parts.values():
                try:
                    a = clarion_rd.parse_record(ab[0])
                    b = clarion_rd.parse_part_b(ab[1])
                except Exception:
                    errors['L2'] += 1
                    continue
                if 'nnodes' in a:
                    L2[clarion_bg.parcel_rowcol(2, *a['code'])] = (a, b)
            for pc, rec in rd_check.shape_records(t, 1).items():
                # plain() right away: the Rec holds an mmap that TileScan closes on exit,
                # so a Rec saved for later use outside this with-block reads from a closed
                # map and silently throws (caught and skipped wherever it's used)
                try:
                    SH1[clarion_bg.parcel_rowcol(1, *pc)] = rec.plain()
                except Exception:
                    errors['SH1'] += 1
                    continue
    return L1, L2, SH1


def load_l2_shapes(paths):
    """{(row, col): plain record} of the level-2 road shapes of tile files or bytes."""
    out = {}
    for p in paths:
        with TileScan(p) as t:
            for pc, rec in rd_check.shape_records(t, 2).items():
                try:
                    out[clarion_bg.parcel_rowcol(2, *pc)] = rec.plain()
                except Exception:
                    continue
    return out


def tiles_format(paths):
    """The format of the first tile, or None when there is none: the tiles of a card share one."""
    for p in paths:
        with TileScan(p) as t:
            return t.fmt


def load_lev(path):
    """{level: {(row,col): (parsed a, parsed part-b)}} for LEV3456R.DAT levels 4 and 5,
    or {} for every level when path is None."""
    if path is None:
        return {}
    data = path if isinstance(path, (bytes, bytearray)) else open(path, 'rb').read()
    lev = lev_file.read(data, decode=True)
    sec = next((s for s in lev['sections'] if s['id'] == 2), None)
    out = {}
    if sec is None:
        return out
    for layer in sec['levels']:
        if layer['level'] not in (4, 5):
            continue
        recs = {}
        two = two_records(lev['fmt'], layer['level'])
        for d in layer['dirs']:
            for row, rr in enumerate(d['rows']):
                rr = rr or []
                if two:
                    for col in range(len(rr) // 2):
                        ra, rb = rr[2 * col], rr[2 * col + 1]
                        if ra and rb:
                            recs[row, col] = (clarion_rd.parse_record(ra['plain']),
                                              clarion_rd.parse_part_b(rb['plain']))
                    continue
                for col, rec in enumerate(rr):
                    if rec:
                        a, b = rec['raw']
                        recs[row, col] = (clarion_rd.parse_record(a), clarion_rd.parse_part_b(b))
        out[layer['level']] = recs
        if layer['level'] == 4 and layer['dirs']:
            # the mesh the level-4 grid starts at; x is stored less 1024
            out['origin'] = (layer['dirs'][0]['y'], (layer['dirs'][0]['x'] + 1024) & 0xffff)
    return out


def load_lev_shapes(path):
    """{level: {(row, col): [(layer, class, links, level-5 word or None, named)]}}, and under
    'codes' {level: {(row, col): {(class, link id): word-3 code}}}, of the
    level-4 and level-5 road shapes, walked as the unit walks them (Navi.exe 0x516604), a
    link as (id, first point, last point, y bits, points), or {} when path is None. Under
    'places', what level3_direction compares: {3: [([(lon, lat)], [y bits])] of each level-3
    shape, 4: [((lon, lat), y bits)] of each level-4 link's middle point}."""
    if path is None:
        return {}
    data = path if isinstance(path, (bytes, bytearray)) else open(path, 'rb').read()
    out = {}
    lev = lev_file.read(data, decode=True)
    for layer in lev['sections'][0]['levels']:
        level = layer['level']
        if level not in (3, 4, 5):
            continue
        recs = out.setdefault(level, {}) if level != 3 else {}
        codes = out.setdefault('codes', {}).setdefault(level, {})
        places = out.setdefault('places', {}).setdefault(level, [])
        for d in layer['dirs']:
            per = d['pairs'] // d['cols']
            x0 = (d['x'] + (lev['fmt'].x_base if level == 3 else 1024)) & 0xffff   # levels 4 and 5 store x less 1024
            for row, rr in enumerate(d['rows'] or []):
                for col in range(d['cols'] if rr else 0):
                    r = rr[col * per]
                    if r is None or 'plain' not in r or len(r['plain']) < 44:
                        continue
                    w = struct.unpack('>%dH' % (len(r['plain']) // 2), r['plain'])
                    if w[2] != 0x00fa or w[7] in (0, 0xffff):
                        continue
                    try:
                        walked = list(coarse_walk(w[2 * w[7]:2 * (w[7] + w[8])]))
                        recs[row, col] = list(coarse_shapes(w[2 * w[7]:2 * (w[7] + w[8])]))
                    except IndexError:
                        recs[row, col] = []
                        continue
                    codes[row, col] = {(cls, lid): code for _, cls, _, _, ids, _, _, code in walked for lid, _ in ids}
                    if level == 5:
                        continue
                    lon0, lat0, pw, ph = clarion_bg.parcel_geo(level, d['y'], x0, row, col)
                    for _, _, _, _, ids, pts, way, _ in walked:
                        geo = [(lon0 + x / 2048 * pw, lat0 + y / 2048 * ph) for x, y in pts]
                        if level == 3:
                            places.append((geo, way))
                            continue
                        for (lid, at), nxt in zip(ids, ids[1:] + [None]):
                            end = nxt[1] if nxt else len(pts) - 1
                            if end - at >= 2:
                                places.append((geo[(at + end) // 2], way[(at + end) // 2]))
    return out


def coarse_walk(u):
    """(layer, class, name word, level-5 words, [(link id, index of its first point)], points,
    y bits, word-3 code) of each shape of one coarse road unit, layer by layer."""
    for layer in range(11):
        p, count = 2 * u[2 * layer], u[2 * layer + 1]
        if u[2 * layer] == 0xffff:
            continue
        for _ in range(count):
            w0, w1, w2, w3, name = u[p:p + 5]
            q = p + 6 + (w3 & 3)
            above = [x for x in u[q:q + (w0 >> 13 & 3)] if x & 0xe000 == fa_encode.UPPER_LEVEL[5]]
            q += (w0 >> 13 & 3) + 1
            ids, pts, way = [], [], []
            for _ in range(w1 & 0xfff):
                x, y = u[q], u[q + 1]
                q += 2 + (1 if y & 0x2000 else 0)
                if y & 0x0800:
                    ids.append((u[q] & 0x1fff, len(pts)))
                    q += 1
                pts.append((x & 0x7ff, y & 0x7ff))
                way.append(y & 0xc000)
            yield layer, w2 >> 12, name, above, ids, pts, way, w3 >> 12
            p += w0 & 0x1fff


def coarse_shapes(u):
    """The shapes of one coarse road unit, layer by layer."""
    for layer, cls, name, above, ids, pts, way, _ in coarse_walk(u):
        links = [(lid, pts[at], pts[nxt[1] if nxt else -1], set(way[at + 1:nxt[1] + 1 if nxt else None]),
                  (nxt[1] if nxt else len(pts) - 1) - at + 1) for (lid, at), nxt in zip(ids, ids[1:] + [None])]
        yield layer, cls, links, above[0] & 0x1fff if above else None, name != 0xffff


# Shared L1 walk (rules 1 and 6): follow the L2 part-B triple's w4 hop through L1.

def l1_parcel(pc2, cell):
    return (pc2[0] * 4 + (cell >> 14), pc2[1] * 4 + ((cell >> 6) & 3))


def graph_len(word, units=rd_encode.LENGTH_UNITS):
    return rd_encode.unscaled(word, units)


def cross(L1, pc, e):
    d = SIDE.get(e['cls'] & 0xe000)
    npc = (pc[0] + d[0], pc[1] + d[1])
    k = e['other'] & 0x3fff
    if npc not in L1 or k >= len(L1[npc]['nodes']):
        return None
    return (npc, k)


def walk_l1(L1, start, nxt, target, budget):
    """Follow L1 from start via the first hop nxt to target. (status, path, length_m)."""
    pc, k = start
    cn = L1[pc]['nodes'][k]
    e0 = [e for e in cn['entries'] if (e['other'] & 0x3fff) == nxt]
    if not e0:
        return 'w4 not adjacent', [start], 0
    e0 = next((e for e in e0 if not e['cls'] & 0x8000), e0[0])
    if e0['cls'] & 0x8000:
        cur = cross(L1, pc, e0)
        if cur is None:
            return 'edge twin missing', [start], 0
        prev, total = start, 0
    else:
        prev, cur, total = start, (pc, nxt), graph_len(e0['b'])
    path = [start, cur]
    cls_path = {e0['cls'] & 0xf}
    for _ in range(MAXSTEPS):
        if cur == target:
            return 'ok', path, total
        pc, k = cur
        n = L1[pc]['nodes'][k]
        if n['nflags'] & 0x2000 and len(path) > 1:
            return 'through another L2 node', path, total

        def back(e):
            return cross(L1, pc, e) == prev if e['cls'] & 0x8000 else (pc, e['other'] & 0x3fff) == prev
        ents = [e for e in n['entries'] if not back(e)]
        real = [e for e in ents if not e['cls'] & 0x8000]
        edges = [e for e in ents if e['cls'] & 0x8000]
        if len(real) > 1:
            same = [e for e in real if (e['cls'] & 0xf) in cls_path]
            if len(same) == 1:
                real = same
            else:
                return 'ambiguous', path, total
        if real:
            e = real[0]
            prev, cur = cur, (pc, e['other'] & 0x3fff)
            total += graph_len(e['b']); cls_path.add(e['cls'] & 0xf)
        elif edges:
            tw = cross(L1, pc, edges[0])
            if tw is None:
                return 'edge twin missing', path, total
            prev, cur = cur, tw
        else:
            return 'dead end', path, total
        path.append(cur)
        if total > budget:
            return 'over budget', path, total
    return 'too long', path, total


def expand_l2_links(L1, L2):
    """Walk every L2 link's L1 path once. Yields (pc2, k, f, status, l1len, l2len, path, cls)
    for links whose own and far block resolve; shared by rules 1 and 6."""
    for pc2, (a, b) in sorted(L2.items()):
        blocks = {x['upper'] & 0x3fff: x for x in b['nodes']}
        for k, n in enumerate(a['nodes']):
            x = blocks.get(k)
            if x is None:
                continue
            own = (l1_parcel(pc2, x['parcel']), x['child'])
            if own[0] not in L1 or x['child'] >= len(L1[own[0]]['nodes']):
                continue
            for e, (w0, w2, w4) in zip(n['entries'], x['entries']):
                if e['cls'] & 0x8000:
                    continue
                f = e['other'] & 0x3fff
                fx = blocks.get(f)
                if fx is None:
                    continue
                tgt = (l1_parcel(pc2, fx['parcel']), fx['child'])
                if tgt[0] not in L1 or fx['child'] >= len(L1[tgt[0]]['nodes']):
                    continue
                l2len = graph_len(e['b'])
                status, path, l1len = walk_l1(L1, own, w4 & 0x3fff, tgt, max(3 * l2len, l2len + 400))
                yield pc2, k, f, status, l1len, l2len, path, e['cls'] & 0xf


# The ten rules.

def result(name, ok, counts, notes=()):
    return dict(name=name, passed=bool(ok), counts=dict(counts), notes=list(notes))


EXPANSION_BAD = 191 / 34976     # the factory's worst tile, QY7 B60R050R; QY8 B50R068R has 22 of 100,658


def check_expansion(L1, L2):
    """Every L2 link unfolds into a continuous, same-class L1 path of about the same
    length. The factory has a few that pass another L2 node or fork on the way, and none
    that ends in a dead end: 0 of 372,268 links on four tiles of both formats."""
    st = collections.Counter()
    for pc2, k, f, status, l1len, l2len, path, cls in expand_l2_links(L1, L2):
        st['links'] += 1
        if status == 'ok':
            r = l1len / l2len if l2len else 0
            if not 0.8 <= r <= 1.25:
                status = 'length off'
        st[status] += 1
    total = st['links']
    bad = total - st['ok']
    ok = bad <= EXPANSION_BAD * total and not st['dead end']
    return result('expansion', ok, st, [
        'bad/total = %d/%d (%.2f%%), pass at the factory share of %.2f%% and no dead end (%d)'
        % (bad, total, 100 * bad / total if total else 0, 100 * EXPANSION_BAD, st['dead end'])])


def check_l2_triple_w4(L1, L2, fmt=None):
    """L2 part-B link triples: w4 is a real entry of the L1 child the triple names, in the
    child's own cell. Factory QY8 is 100%. QY7 has a second form where the child is an edge
    node and the link runs on across the line: the next parcel's cell and the node at the
    child's point there (4,539 of 4,539 triples with a foreign cell on B60R050R and B50R068R)."""
    across = fmt is not None and fmt.next_across
    st = collections.Counter()
    bad = []
    for pc, (a, b) in L2.items():
        for x in b['nodes']:
            k = x['upper'] & 0x3fff
            own_pc = (pc[0] * 4 + (x['parcel'] >> 14), pc[1] * 4 + (x['parcel'] >> 6 & 3))
            own1 = L1.get(own_pc)
            cn = own1['nodes'][x['child']] if own1 and x['child'] < len(own1['nodes']) else None
            if k >= len(a['nodes']):
                continue
            for e, (w0, w2, w4) in zip(a['nodes'][k]['entries'], x['entries']):
                if e['cls'] & 0x8000:
                    continue
                st['link'] += 1
                if across and cn is not None and w2 != x['parcel']:
                    there = (pc[0] * 4 + (w2 >> 14), pc[1] * 4 + (w2 >> 6 & 3))
                    n = L1[there]['nodes'][w4] if there in L1 and w4 < len(L1[there]['nodes']) else None
                    twin = (n is not None and l1_units(there, n['x'], n['y']) == l1_units(own_pc, cn['x'], cn['y'])
                            and any(z['cls'] & 0x8000 and z['other'] & 0x3fff == w4 for z in cn['entries']))
                    st['names the twin across a parcel line'] += twin
                    if not twin:
                        bad.append((pc, k, x['child'], w2, w4))
                    continue
                st['w2==own cell'] += w2 == x['parcel']
                if cn is None:
                    st['child unresolved'] += 1
                    bad.append((pc, k))
                    continue
                ok = any((z['other'] & 0x3fff) == w4 for z in cn['entries'])
                st['w4 adjacent to own L1 child'] += ok
                if not ok:
                    bad.append((pc, k, x['child'], w4))
    plain = st['link'] - st['names the twin across a parcel line']
    passed = not bad and st['w4 adjacent to own L1 child'] == plain and st['w2==own cell'] == plain
    return result('l2_triple_w4', passed, st, ['bad examples: %r' % bad[:5]])


def check_edges(L1, L2, lev4, lev5):
    """Edge entries: travel-time value zero at L2/L4/L5 (not required at L1), exactly one
    real link per edge node, and the part-B edge triple's w2/w4 name the mirrored block
    (L1/L2 mirroring, L4/L5 w2/w4, and the time field)."""
    st = collections.Counter()
    bad = []

    def degree(level, recs):
        for pc, a in recs.items():
            for i, n in enumerate(a['nodes']):
                ends = [e for e in n['entries'] if e['cls'] & 0x8000]
                if not ends:
                    continue
                real = len(n['entries']) - len(ends)
                st['%d:border nodes' % level] += 1
                st['%d:border nodes >1 real link' % level] += real > 1
                if real > 1 and len(bad) < 20:
                    bad.append((level, pc, i, 'real', real))
                for e in ends:
                    st['%d:edge entries' % level] += 1
                    if level != 1:
                        st['%d:edge time zero' % level] += (e['a'] & 0xfff) == 0

    def mirror_l1l2(level, recs):
        for pc, a in recs.items():
            for i, n in enumerate(a['nodes']):
                for e in n['entries']:
                    if not e['cls'] & 0x8000:
                        continue
                    dr, dc = SIDE[e['cls'] & 0xe000]
                    far = recs.get((pc[0] + dr, pc[1] + dc))
                    fi = e['other'] & 0x3fff
                    if far is None or fi >= len(far['nodes']):
                        # neighbour parcel not in the given tile set (tile/database edge) -
                        # not a defect by itself, only reported, since a single tile or a
                        # database boundary legitimately has no record on the other side
                        st['%d:edges off the given tiles' % level] += 1
                        continue
                    back = [z for z in far['nodes'][fi]['entries']
                            if z['cls'] & 0x8000 and (z['cls'] & 0xe000) == OPPOSITE[e['cls'] & 0xe000]
                            and (z['other'] & 0x3fff) == i]
                    if not back:
                        st['%d:edges unmirrored' % level] += 1
                        if len(bad) < 20:
                            bad.append((level, pc, i, 'unmirrored'))

    def mirror_l45(level, recs):
        for (row, col), (a, b) in recs.items():
            blocks = {x['upper'] & 0x3fff: x for x in b['nodes']}
            for k, n in enumerate(a['nodes']):
                x = blocks.get(k)
                if x is None:
                    continue
                for e, (w0, w2, w4) in zip(n['entries'], x['entries']):
                    if not e['cls'] & 0x8000:
                        continue
                    dr, dc = SIDE[e['cls'] & 0xe000]
                    far = recs.get((row + dr, col + dc))
                    if far is None:
                        st['%d:edge far missing' % level] += 1
                        continue
                    fb = {y['upper'] & 0x3fff: y for y in far[1]['nodes']}.get(w0 & 0x3fff)
                    if fb is None:
                        st['%d:edge far missing' % level] += 1
                        continue
                    st['%d:edge w2/w4==twin' % level] += (w2 == fb['parcel'] and w4 == fb['child'])
                    st['%d:edge entries' % level] += 1
                    st['%d:edge time zero' % level] += (e['a'] & 0xfff) == 0

    degree(1, L1); degree(2, L2nodes(L2))
    mirror_l1l2(1, L1); mirror_l1l2(2, L2nodes(L2))
    for level, recs in ((4, lev4), (5, lev5)):
        for (row, col), (a, b) in recs.items():
            for i, n in enumerate(a['nodes']):
                ends = [e for e in n['entries'] if e['cls'] & 0x8000]
                if not ends:
                    continue
                real = len(n['entries']) - len(ends)
                st['%d:border nodes' % level] += 1
                st['%d:border nodes >1 real link' % level] += real > 1
        mirror_l45(level, recs)

    multi = sum(st[k] for k in st if k.endswith('border nodes >1 real link'))
    unmirrored = sum(st[k] for k in st if k.endswith('edges unmirrored'))
    time_edges = sum(st[k] for k in st if k.endswith('edge entries') and not k.startswith('1:'))
    time_zero = sum(st[k] for k in st if k.endswith('edge time zero'))
    w2w4 = sum(st[k] for k in st if k.endswith('w2/w4==twin'))
    w2w4_total = sum(st[k] for k in st if k.endswith('edge entries') and (k.startswith('4:') or k.startswith('5:')))
    ok = multi == 0 and unmirrored == 0 and time_zero == time_edges and (w2w4_total == 0 or w2w4 == w2w4_total)
    return result('edge_entries', ok, st, list(bad[:5]))


def check_l2_edge_triples(L2):
    """A level-2 edge triple names the far node's own part-B block: its cell word and its
    level-1 child. Factory QY7 B58R048R, B58R050R, B60R048R, B60R050R: 31,826 of 31,826,
    330 of them across a tile line. QY8 B50R068R, B58R060R, B58R068R, B60R060R: 5,324 of 5,324."""
    st = collections.Counter()
    bad = []
    for pc, (a, b) in L2.items():
        if b is None:
            continue
        blocks = {x['upper'] & 0x3fff: x for x in b['nodes']}
        for k, n in enumerate(a['nodes']):
            x = blocks.get(k)
            for j, e in enumerate(n['entries']):
                if not e['cls'] & 0x8000:
                    continue
                st['edge triples'] += 1
                dr, dc = SIDE[e['cls'] & 0xe000]
                far = L2.get((pc[0] + dr, pc[1] + dc))
                if far is None or far[1] is None:
                    st['far parcel off the given tiles'] += 1
                    continue
                fb = {y['upper'] & 0x3fff: y for y in far[1]['nodes']}.get(e['other'] & 0x3fff)
                if x is not None and fb is not None and j < len(x['entries']) and tuple(x['entries'][j][1:]) == (fb['parcel'], fb['child']):
                    st['the far block'] += 1
                else:
                    st['not the far block'] += 1
                    bad.append((pc, k, j))
    return result('l2_edge_triples', not bad, st, ['wrong: %r' % bad[:5]])


def check_l2_block_order(L2):
    """A level-2 record's part-B blocks run by cell, then by child node, in both formats.
    Factory: 2,948 of 2,948 records of QY7 B58R050R, 2,005 of 2,005 of QY8 B48R060R."""
    st = collections.Counter()
    bad = []
    for pc, (a, b) in L2.items():
        if b is None:
            continue
        keys = [(x['parcel'], x['child']) for x in b['nodes']]
        st['records'] += 1
        if keys != sorted(keys):
            st['out of child order'] += 1
            bad.append(pc)
    return result('l2_block_order', not bad, st, ['wrong: %r' % bad[:5]])


def L2nodes(L2):
    """L2's (a) parts keyed the same way mirror_l1l2 wants: {pc: parsed record}."""
    return {pc: a for pc, (a, b) in L2.items()}


def check_no_junction_on_parcel_line(L1, L2):
    """No junction (a node with more than one real link) sits on a parcel boundary - it
    would need an edge entry there and the factory never gives a border node more than
    one real link (the generic form of the card-068 'junctions quantized onto
    a parcel line' bug, without Iceland-specific coordinates)."""
    st = collections.Counter()
    bad = []
    for level, recs in ((1, L1), (2, L2nodes(L2))):
        for pc, a in recs.items():
            for i, n in enumerate(a['nodes']):
                ends = [e for e in n['entries'] if e['cls'] & 0x8000]
                if not ends:
                    continue
                real = len(n['entries']) - len(ends)
                st['%d:border nodes' % level] += 1
                if real > 1:
                    st['%d:junctions on a parcel line' % level] += 1
                    if len(bad) < 10:
                        bad.append((level, pc, i, n['x'], n['y'], real))
    bad_total = sum(v for k, v in st.items() if 'junctions on a parcel line' in k)
    return result('no_junction_on_parcel_line', bad_total == 0, st, bad)


COARSE_ORDERED = 509 / 513      # QY8 level-4 records in class-then-x order; level 5 has 42 of 42


def check_node_order(lev4, lev5, L1=None, L2=None):
    """Nodes of a record are in class order, and ours by x then y within a class.
    Factory, class: every level-1 and level-2 record of five tiles in both formats, 509 of
    513 and 380 of 385 at level 4. QY8 orders a class by x as well (47,192 of 47,192 level-1
    records with y after it, 1,689 of 1,689 at level 2, 509 of 513 at level 4, y unordered
    from level 2 up). QY7 does not order a class by x; the writer does on both."""
    st = collections.Counter()
    bad = []
    for level, recs in ((1, L1 or {}), (2, L2nodes(L2 or {})),
                        (4, {pc: a for pc, (a, b) in lev4.items()}),
                        (5, {pc: a for pc, (a, b) in lev5.items()})):
        for pc, a in recs.items():
            keys = [(n['ncls'], n['x'], n['y']) for n in a['nodes']]
            st['%d:records' % level] += 1
            if keys == sorted(keys):
                st['%d:ordered' % level] += 1
            elif len(bad) < 10:
                bad.append((level, pc))
    fine = sum(st['%d:ordered' % lv] == st['%d:records' % lv] for lv in (1, 2))
    coarse = st['4:records'] + st['5:records']
    ok = fine == 2 and st['4:ordered'] + st['5:ordered'] >= COARSE_ORDERED * coarse
    return result('node_order', ok, st, bad)


def check_l1_link_direction(L1, L2):
    """L1 link direction: an entry on a chain a successfully-expanded L2 link walks
    through carries w4 & 0x6 != 0; an off-chain side link at the same node has it clear;
    classes 8, 9 and 12 (level-1-only roads, never carried at level 2) never carry it.
    (correlation measured, not a factory-coded rule, so
    the threshold here is a ratio, not exact)."""
    chain_set = chain_clear = side_set = side_clear = 0
    for pc2, k, f, status, l1len, l2len, path, cls in expand_l2_links(L1, L2):
        if status != 'ok':
            continue
        for i in range(len(path) - 1):
            pc, kk = path[i]
            npc, nk = path[i + 1]
            if pc != npc:
                continue
            nd = L1[pc]['nodes'][kk]
            for z in nd['entries']:
                if z['cls'] & 0x8000:
                    continue
                o = z['other'] & 0x3fff
                if o == nk:
                    if z['w4'] & 6:
                        chain_set += 1
                    else:
                        chain_clear += 1
                elif i > 0 and (pc, o) != path[i - 1]:
                    if z['w4'] & 6:
                        side_set += 1
                    else:
                        side_clear += 1
    never = collections.Counter()
    for pc, a in L1.items():
        for nd in a['nodes']:
            for z in nd['entries']:
                if z['cls'] & 0x8000:
                    continue
                cls = z['cls'] & 0xf
                if cls in (8, 9, 12) and z['w4'] & 6:
                    never[cls] += 1
    st = collections.Counter(chain_w4_set=chain_set, chain_w4_clear=chain_clear,
                              side_w4_set=side_set, side_w4_clear=side_clear,
                              never_classes_violations=sum(never.values()))
    chain_total = chain_set + chain_clear
    side_total = side_set + side_clear
    ok = (sum(never.values()) == 0 and
          (chain_total == 0 or chain_set / chain_total >= 0.9) and
          (side_total == 0 or side_set / side_total <= 0.1))
    return result('l1_link_direction', ok, st,
                  ['classes 8/9/12 with w4&6 set: %r (want 0)' % dict(never)])


def check_l2_nflags(L2, fmt=None):
    """L2 nodes carry the hierarchy flags 0x4000/0x2000 (lev_graph.patch_l2_flags). Report
    counts; fail if the tile has L2 nodes at all but neither flag ever appears - a bug
    an earlier card had - or if a QY7 tile has 0x4000, which its factory tiles never do
    on a node with a level above (0 of 49,840 on B48R060R)."""
    st = collections.Counter()
    for pc, a in L2nodes(L2).items():
        for n in a['nodes']:
            st['nodes'] += 1
            st['nflags 0x4000'] += bool(n['nflags'] & 0x4000)
            st['nflags 0x2000'] += bool(n['nflags'] & 0x2000)
            st['nflags other bits'] += bool(n['nflags'] & ~0x6000)
    ok = st['nodes'] == 0 or st['nflags 0x4000'] + st['nflags 0x2000'] > 0
    if fmt is not None and not fmt.level_count:
        ok = ok and not st['nflags 0x4000']
    return result('l2_nflags', ok, st)


def check_roundabouts(L1, L2, SH1, fmt=None):
    """Roundabout ring entries carry w4 & 0x7000 == 0x7000 at L1 and L2, and a section-1
    ring string's header word 3 (fa_encode.shape_words: cls, ring-flag, name, ...) is
    0x7004 on QY8 and 0x7000 on QY7. Not a clean 100%% invariant even on factory - Koln and a UK tile disagree
    on it - so this reports counts and passes if the two sides are at least mutually
    consistent where both are present."""
    st = collections.Counter()
    ring_nodes = {1: set(), 2: set()}
    for level, recs in ((1, L1), (2, L2nodes(L2))):
        for pc, a in recs.items():
            for n in a['nodes']:
                ring = any(e['w4'] & 0x7000 == 0x7000 for e in n['entries'] if not e['cls'] & 0x8000)
                st['%d:nodes' % level] += 1
                st['%d:ring nodes (w4 mask)' % level] += ring
                if ring:
                    ring_nodes[level].add((pc, n['x'], n['y']))
    for pc, plain in SH1.items():
        try:
            code, g, shapes = fa_encode.parse_record(plain, 1)
        except Exception:
            continue
        w = struct.unpack('>%dH' % (len(plain) // 2), plain)
        for sh in shapes:
            if not sh['ok']:
                continue
            hdr3 = w[sh['at'] + 3] if sh['at'] + 3 < len(w) else None
            flagged = hdr3 == 0x7000 | (fmt or clarion_format.DEFAULT).shape_w3   # 0x7004 on QY8, 0x7000 on QY7
            st['1:section-1 shapes'] += 1
            st['1:section-1 ring strings (code 7 in word 3)'] += flagged
            if flagged:
                ends = sh['ends']
                both_ring = all((p in {(x, y) for pc2, x, y in ring_nodes[1]}) for pair in ends for p in pair)
                st['1:ring strings with ring-flagged entries at both ends'] += both_ring
    # the w4&0x7000 mask and the header-word-3 flag should agree on every ring string this
    # writer produced; whether the factory's OWN two fields agree is unsettled (Koln and
    # a UK tile disagree), so this only gates on our own internal consistency
    total_rings = st['1:section-1 ring strings (code 7 in word 3)']
    consistent = st['1:ring strings with ring-flagged entries at both ends']
    ok = total_rings == 0 or consistent == total_rings
    return result('roundabouts', ok, st)


def check_shape_link_codes(L1, L2, SH1, SH2):
    """A shape's link code, the top nibble of word 3, is the w4 code of each of its links
    in the graph, at levels 1 and 2. Factory: 3,812,205 of 3,812,205 level-1 links and
    187,373 of 187,373 level-2 links on five tiles of both formats, codes 0 to 9."""
    st = collections.Counter()
    bad = []
    l1 = {pc: (w, shapes) for pc, w, shapes in road_shapes(SH1)}
    l2 = {}
    for pc, pl in (SH2 or {}).items():
        try:
            l2[pc] = (struct.unpack('>%dH' % (len(pl) // 2), pl), fa_encode.parse_record(pl, 2)[2])
        except Exception:
            continue
    for level, recs, drawn in ((1, L1, l1), (2, L2nodes(L2), l2)):
        for pc, (w, shapes) in drawn.items():
            if pc not in recs:
                continue
            code = {(e['cls'] & 0xf, e['id'] & 0x1fff): e['w4'] >> 12
                    for n in recs[pc]['nodes'] for e in n['entries'] if not e['cls'] & 0x8000}
            for S in shapes:
                for lid in S['links'] if S['length'] else ():
                    g = code.get((S['cls'], lid & 0x1fff))
                    if g is None:
                        st['%d:shape links of no graph link' % level] += 1
                        continue
                    st['%d:shape links' % level] += 1
                    if g != w[S['at'] + 3] >> 12:
                        st['%d:code differs' % level] += 1
                        bad.append((level, pc, S['cls'], lid & 0x1fff, w[S['at'] + 3] >> 12, g))
    return result('shape_link_codes', not bad, st, ['wrong (level, parcel, class, link, shape code, graph code): %r' % bad[:5]])


def check_graph_words(L1, L2, lev4, lev5, fmt=None):
    """A link entry's class word and regulation words have the format's form at every level.
    X08-03: bit 7 set in the class word, 0x10 under class 10, exit nibble 0, the arriving end of
    a one-way link barred from every entry (f0ff). X08-02: bit 7 clear, 0x10 under class 11,
    exit nibble 3, no word on an arriving end and none from a leaving end to itself (stock
    LEV3456R and B58R050R: 402,607 of 402,608 arriving ends, 293,371 of 293,374 words)."""
    fmt = fmt or clarion_format.DEFAULT
    st = collections.Counter()
    bad = []
    levels = ((1, L1), (2, L2nodes(L2)), (4, {pc: a for pc, (a, b) in lev4.items()}), (5, {pc: a for pc, (a, b) in lev5.items()}))
    for level, recs in levels:
        for pc, a in recs.items():
            for k, nd in enumerate(a['nodes']):
                for j, e in enumerate(nd['entries']):
                    if e['cls'] & 0x8000:
                        continue
                    st['L%d entries' % level] += 1
                    cls = e['cls'] & 0xf
                    wrong = []
                    if e['cls'] & 0xf0 != fmt.class_flag | (0x10 if cls < fmt.class_plain else 0):
                        wrong.append('class word')
                    words = e['extra'][:(e['w5'] >> 6) & 0x3f]
                    if any(w >> 8 & 0xf != fmt.reg_exit for w in words):
                        wrong.append('regulation exit nibble')
                    arriving, leaving = e['w5'] & 0xc000 == 0x8000, e['w5'] & 0xc000 == 0x4000
                    if fmt.oneway_end_words:
                        if arriving and not any(w >> 12 == 15 for w in words):
                            wrong.append('arriving end with no word from every entry')
                    else:
                        if arriving and words:
                            wrong.append('arriving end with a word')
                        if leaving and any(w >> 12 == j for w in words):
                            wrong.append('leaving end with a word from itself')
                    for what in wrong:
                        st['L%d %s' % (level, what)] += 1
                    if wrong and len(bad) < 5:
                        bad.append((level, pc, k, j, wrong))
    return result('graph_words', not any(' entries' not in k and v for k, v in st.items()), st, ['wrong: %r' % bad])


def check_length_time_units(L1, L2):
    """Length/time words use a doubling multiplier (1,2,4,32 m; 1,2,4,64 x0.1s -
    rd_encode.LENGTH_UNITS/TIME_UNITS, travel-time-encoding memory note), and an L2
    word's value should read back close to the summed L1 path under that table.
    (LEV L4/L5 vs straight-line distance, needs each record's own
    parcel geometry and is not ported here - this covers the L2-vs-L1 half only.)"""
    ratios = []
    for pc2, k, f, status, l1len, l2len, path, cls in expand_l2_links(L1, L2):
        if status == 'ok' and l1len >= 20:
            ratios.append(l1len / l2len if l2len else 0)
    st = collections.Counter()
    n = len(ratios)
    med = sorted(ratios)[n // 2] if n else None
    ok = n == 0 or 0.85 <= med <= 1.2
    st['l2_vs_l1_links_compared'] = n
    if med is not None:
        st['median_ratio_x1000'] = round(med * 1000)
    return result('length_time_units', ok, st)


def check_coarse_graph_present(L2, lev4, lev5):
    """LEV3456R.DAT carries L4 and L5 routing records once the tile's roads span more
    than one L2 parcel (a graph should exist wherever there
    is more than one L2 window of road data to connect)."""
    l2_windows = len(L2nodes(L2))
    st = collections.Counter(l2_windows=l2_windows, l4_records=len(lev4), l5_records=len(lev5))
    needs_graph = l2_windows > 1
    ok = (not needs_graph) or (len(lev4) > 0 and len(lev5) > 0)
    return result('coarse_graph_present', ok, st)


def upper_shapes(SH1, code=0x2000, span=4, origin=(0, 0)):
    """{(parcel above, class, link id): [(start, end)]} of the level-1 shapes whose header names
    the link with this level code, in level-1 units counted from parcel (0, 0). span is the
    upper parcel's side in level-1 parcels and origin the level-1 parcel its grid starts at.
    A point on a parcel's far edge is the next parcel's 0."""
    out = collections.defaultdict(list)
    for pc, w, shapes in road_shapes(SH1):
        above = (pc[0] - origin[0]) // span, (pc[1] - origin[1]) // span
        for S in shapes:
            q = S['at'] + 6 + (w[S['at'] + 3] & 3)
            ids = [u & 0x1fff for u in w[q:q + (w[S['at']] >> 13)] if u & 0xe000 == code]
            if ids and S['ends']:
                out[above + (S['cls'], ids[0])].append((l1_units(pc, *S['ends'][0][0]), l1_units(pc, *S['ends'][-1][1])))
    return out


def road_shapes(SH1):
    """(parcel, record words, shapes) of each level-1 road-shape record. One whose layer table
    gives a shape of no length is of another form and is left out: 31 of 14,367 on factory
    B50R068R, none of ours."""
    for pc, pl in SH1.items():
        try:
            shapes = fa_encode.parse_record(pl, 1)[2]
        except Exception:
            continue
        if all(S['length'] for S in shapes):
            yield pc, struct.unpack('>%dH' % (len(pl) // 2), pl), shapes


def l1_units(pc, x, y):
    return pc[1] * 2048 + x + (x == 2047), pc[0] * 2048 + y + (y == 2047)


def blocks_of(b):
    return {x['upper'] & 0x3fff: x for x in b['nodes']} if b else {}


def l1_node(L1, pc2, block):
    """Where the level-1 node under a level-2 node sits (l1_units), or None when its record
    is not loaded."""
    pc1 = l1_parcel(pc2, block['parcel']) if block else None
    if pc1 not in L1 or block['child'] >= len(L1[pc1]['nodes']):
        return None
    m = L1[pc1]['nodes'][block['child']]
    return l1_units(pc1, m['x'], m['y'])


def child_parcel(level, par, word, origin):
    """The parcel a level-4 or level-5 part-B block names, one level down."""
    if level == 4:
        return par[0] * 16 + origin[0] * 8 + (word >> 12), par[1] * 16 + origin[1] * 8 + (word >> 4 & 0xf)
    return par[0] * 4 + (word >> 14), par[1] * 4 + (word >> 6 & 3)


def walk_shapes(shapes, first, loaded=None):
    """Follow the shapes naming one upper link from the level-1 node under its first node,
    the way the unit does: the shape that starts where the last one ended (Navi.exe 0x34d2b8).
    With the loaded level-1 parcels given, a walk that ends at one not loaded, with no shape
    left that is near, says so."""
    left = list(shapes)
    pos = first
    if not any(s == pos for s, e in left):
        return 'backwards' if any(e == pos for s, e in left) else 'no shape at the first node'
    while True:
        nxt = next((g for g in left if g[0] == pos), None)
        if nxt is None:
            break
        left.remove(nxt)
        pos = nxt[1]
    if not left:
        return 'walked'
    off = loaded is not None and any(((pos[1] - dy) // 2048, (pos[0] - dx) // 2048) not in loaded for dx in (0, 1) for dy in (0, 1))
    if off and not any(max(abs(p[0] - pos[0]), abs(p[1] - pos[1])) <= GAP for g in left for p in g):
        return 'walk leaves the loaded tiles'
    return 'backwards' if any(e == pos for s, e in left) else 'walk stops short'


def first_ends(a):
    """[(class, link id, node index)] of the first node of each link of one graph record."""
    return [(e['cls'] & 0xf, e['id'] & 0x1fff, k) for k, nd in enumerate(a['nodes']) for e in nd['entries']
            if not e['cls'] & 0x8000 and not e['id'] & 0x2000]


def upper_link_walks(L1, L2, SH1, loaded=None):
    """(level-2 parcel, class, link id, how the walk ends) of each level-2 link over loaded
    level-1 records: walk_shapes over the level-1 shapes naming it, or 'not named'."""
    named = upper_shapes(SH1)
    for pc2, (a, b) in L2.items():
        blocks = blocks_of(b)
        for cls, lid, k in first_ends(a):
            node = l1_node(L1, pc2, blocks.get(k))
            if node:
                key = pc2 + (cls, lid)
                yield pc2, cls, lid, walk_shapes(named[key], node, loaded) if key in named else 'not named'


def check_upper_link_shapes(L1, L2, SH1):
    """The level-1 shapes naming a level-2 link run the way the link runs: the unit takes
    the shape that starts where the last one ended (Navi.exe 0x34d2b8), so one drawn
    backwards ends its walk. Factory B50R068R: none backwards on 46,835 links."""
    st = collections.Counter()
    for pc2, cls, lid, status in upper_link_walks(L1, L2, SH1):
        if status != 'not named':
            st['links named'] += 1
            st[status] += 1
    return result('upper_link_shapes', st['backwards'] == 0, st,
                  ['level-2 links with a level-1 shape drawn backwards: %d (want 0)' % st['backwards']])


def shape_headers(SH1):
    """(level-1 parcel, class, point-count word, upper-link words) of every level-1 road shape."""
    for pc, w, shapes in road_shapes(SH1):
        for S in shapes:
            q = S['at'] + 6 + (w[S['at'] + 3] & 3)
            yield pc, S['cls'], w[S['at'] + 1], w[q:q + (w[S['at']] >> 13)]


def check_l2_nodes_on_l1(L1, L2, tol=8):
    """Every level-2 node sits on the level-1 node its block names, and that node carries
    0x2000: a level-2 link is whole level-1 links. Factory B50R068R: 41,875 of 41,875 within
    8 level-1 units, all flagged."""
    st = collections.Counter()
    for pc2, (a, b) in L2.items():
        blocks = {x['upper'] & 0x3fff: x for x in b['nodes']}
        for k, n in enumerate(a['nodes']):
            x = blocks.get(k)
            pc1 = l1_parcel(pc2, x['parcel']) if x else None
            if pc1 not in L1 or x['child'] >= len(L1[pc1]['nodes']):
                continue
            m = L1[pc1]['nodes'][x['child']]
            st['nodes'] += 1
            off = max(abs(m['x'] - 4 * n['x'] + 2048 * (pc1[1] - 4 * pc2[1])),
                      abs(m['y'] - 4 * n['y'] + 2048 * (pc1[0] - 4 * pc2[0])))
            if off > tol:
                st['inside a level-1 link'] += 1
            elif not m['nflags'] & 0x2000:
                st['level-1 node not flagged'] += 1
    bad = st['inside a level-1 link'] + st['level-1 node not flagged']
    return result('l2_nodes_on_l1', bad <= 0.0005 * st['nodes'], st,
                  ['level-2 nodes off their level-1 node or on an unflagged one: %d (factory 0, pass at 0.05%%)' % bad])


def check_traffic_shapes(SH1, none=0.01, some=0.4, streets=0.02):
    """Bit 14 of a shape's point-count word marks a road of the country's traffic (TMC)
    location table; the traffic module lists only such shapes (Navi.exe 0x4de924). Factory
    class 0-6 shapes: 50% to 100% set in thirteen countries with a table, 0.2% to 0.6% in
    Belarus, Albania and North Macedonia, which have none; class 8 and up 0.02% to 1.2%.
    A card is one or the other."""
    st = collections.Counter()
    for pc, cls, w1, words in shape_headers(SH1):
        grp = 'main' if cls <= 6 else 'street' if cls >= 8 and cls != 10 else None
        if grp:
            st[grp] += 1
            st[grp + ' set'] += bool(w1 & fa_encode.TRAFFIC)
    ok = (st['main set'] <= none * st['main'] or st['main set'] >= some * st['main']) and st['street set'] <= streets * st['street']
    return result('traffic_shapes', ok, st, ['set on %d of %d class 0-6 shapes (want under %d%% or over %d%%), %d of %d class 8+ (want %d%% or less)'
                                             % (st['main set'], st['main'], 100 * none, 100 * some, st['street set'], st['street'], 100 * streets)])


def check_hierarchy_codes(L1, SH1, fmt=None):
    """Bits 1-2 of a level-1 link's w4 count the levels above that have the link: the number
    of upper-link words in its shape header. Factory B50R068R: 2,192,458 of 2,192,458 entries.
    On QY7 the code is 1 for any number of words: 4,750,926 of 4,750,926 on factory B58R050R,
    72,090 of them with two or three words."""
    count = fmt is None or fmt.level_count
    words = {}
    for pc, w, shapes in road_shapes(SH1):
        for S in shapes:
            for lid in S['links']:
                words[pc, S['cls'], lid & 0x1fff] = w[S['at']] >> 13
    st = collections.Counter()
    for pc, a in L1.items():
        for nd in a['nodes']:
            for e in nd['entries']:
                n = words.get((pc, e['cls'] & 0xf, e['id'] & 0x1fff))
                if e['cls'] & 0x8000 or n is None:
                    continue
                st['entries'] += 1
                st['code is not the word count'] += (e['w4'] >> 1 & 3) != (n if count else min(n, 1))
    return result('hierarchy_codes', not st['code is not the word count'], st,
                  ['link entries whose w4 code is not %s: %d (factory 0 of 7,573,540 on four tiles, pass at none)'
                   % ('their shape\'s word count' if count else '1 under a shape with words, else 0', st['code is not the word count'])])


COARSE_SPAN = {4: 64, 5: 256}      # a coarse parcel's side in level-1 parcels


def coarse_link_walks(L1, L2, SH1, lev4, lev5, origin, loaded=None):
    """(level, parcel, class, link id, how the walk ends) of each level-4 and level-5 link
    over loaded tiles: walk_shapes over the level-1 shapes naming it, or 'not named'."""
    def under(level, par, k):
        """The level-1 node under node k of a level-4 or level-5 record."""
        rec = (lev4 if level == 4 else lev5).get(par)
        block = blocks_of(rec[1]).get(k) if rec else None
        if block is None:
            return None
        child = child_parcel(level, par, block['parcel'], origin)
        if level == 5:
            return under(4, child, block['child'])
        return l1_node(L1, child, blocks_of(L2[child][1]).get(block['child'])) if child in L2 else None

    for lv, recs in ((4, lev4), (5, lev5)):
        named = upper_shapes(SH1, fa_encode.UPPER_LEVEL[lv], COARSE_SPAN[lv], (origin[0] * 32, origin[1] * 32))
        for par, (a, b) in recs.items():
            for cls, lid, k in first_ends(a):
                node = under(lv, par, k)
                if node is not None:
                    key = par + (cls, lid)
                    yield lv, par, cls, lid, walk_shapes(named[key], node, loaded) if key in named else 'not named'


def check_coarse_link_words(L1, L2, SH1, lev4, lev5, origin):
    """A level-1 shape names its level-5, level-4 and level-2 links, highest first, each
    level-4 link is named from below, and the shapes naming a coarse link run its way."""
    st = collections.Counter()
    if not lev4 or origin is None:
        return result('coarse_link_words', True, st, ['no coarse graph loaded, nothing to check'])
    order = {0x8000: 0, 0x6000: 1, 0x2000: 2}
    for pc, cls, w1, words in shape_headers(SH1):
        if not words:
            continue
        codes = [u & 0xe000 for u in words]
        st['shapes with words'] += 1
        if codes != sorted(codes, key=lambda c: order.get(c, 3)) or codes[-1] != 0x2000 or (0x8000 in codes and 0x6000 not in codes):
            st['words out of order'] += 1

    for lv, recs in ((4, lev4), (5, lev5)):
        named = upper_shapes(SH1, fa_encode.UPPER_LEVEL[lv], COARSE_SPAN[lv], (origin[0] * 32, origin[1] * 32))
        links = {par + key for par, (a, b) in recs.items() for key in {(cls, lid) for cls, lid, k in first_ends(a)}}
        st['L%d names of no link' % lv] = sum(key[:2] in recs and key not in links for key in named)
    for lv, par, cls, lid, status in coarse_link_walks(L1, L2, SH1, lev4, lev5, origin):
        st['L%d links' % lv] += 1
        st['L%d %s' % (lv, status)] += 1
    n = st['L4 links'] + st['L5 links']
    # factory, QY7 B60R050R and QY8 B50R068R: no words out of order on 136,011 shapes, no name
    # of no link, none backwards; 852 of 9,067 level-4 links unnamed on the QY8 tile, 0 on the other
    ok = (not st['words out of order']
          and not st['L4 names of no link'] + st['L5 names of no link']
          and not st['L4 backwards'] + st['L5 backwards']
          and st['L4 not named'] <= 852 / 9067 * st['L4 links'])
    return result('coarse_link_words', ok, st,
                  ['level-4 links no level-1 shape names: %d of %d (want the factory share of 9.4%% or less); coarse links with a shape drawn backwards: %d (want none)'
                   % (st['L4 not named'], st['L4 links'], st['L4 backwards'] + st['L5 backwards'])])


LINK_POINTS = 32          # the most one coarse link has on the factory card
# the stock cards' own shares, levels 4 and 5 together: QY8 (796,572 links) and QY7 (547,701)
LOOSE = 335 / 796572      # links without a shape, shapes without a link; QY7 224
APART = 416 / 547701      # shape links not between their link's nodes; QY8 329
NO_LEVEL5_WORD = 8 / 138453   # level-4 shapes without or with a wrong level-5 word; QY8 1 of 198,137
WRONG_WAY = {False: 11 / 547701, True: 12283 / 796572}   # one-way bits not the link's, QY7 and QY8


def check_coarse_shapes(lev4, lev5, coarse, fmt=None):
    """The level-4 and level-5 road shapes are the graph's links, drawn: every graph link has
    a shape link of its class and id running between its two nodes and every shape link a
    graph link, so no road the graph carries has a gap. A level-4 shape names its level-5
    link when the graph says level 5 has it, a layer keeps each class together, the points
    of a link carry its one-way bits and number 32 at most, and the numbered classes carry
    their route numbers."""
    st = collections.Counter()
    if not coarse or not lev4:
        return result('coarse_shapes', True, st, ['no coarse road shapes loaded, nothing to check'])
    keys5 = {par + (e['cls'] & 0xf, e['id'] & 0x1fff) for par, (a, b) in lev5.items()
             for nd in a['nodes'] for e in nd['entries'] if not e['cls'] & 0x8000}
    for lv, recs in ((4, lev4), (5, lev5)):
        for par, (a, b) in recs.items():
            ends = collections.defaultdict(dict)
            for nd in a['nodes']:
                for e in nd['entries']:
                    if not e['cls'] & 0x8000:
                        ends[e['cls'] & 0xf, e['id'] & 0x1fff]['E' if e['id'] & 0x2000 else 'S'] = ((nd['x'], nd['y']), e['w4'] >> 1 & 3, e['w5'] & 0xc000)
            seen, layers = set(), collections.defaultdict(list)
            for layer, cls, links, above, named in coarse.get(lv, {}).get(par, ()):
                layers[layer].append(cls)
                for lid, first, last, way, npts in links:
                    st['L%d shape links' % lv] += 1
                    st['links of more than %d points' % LINK_POINTS] += npts > LINK_POINTS
                    if cls in (3, 4, 5):
                        st['L%d numbered-class links' % lv] += 1
                        st['L%d with a route number' % lv] += named
                    g = ends.get((cls, lid))
                    if g is None or (cls, lid) in seen:
                        st['L%d shape links of no graph link' % lv] += 1
                        continue
                    seen.add((cls, lid))
                    if g.get('S', (None,))[0] != first or g.get('E', (None,))[0] != last:
                        st['L%d not node to node' % lv] += 1
                    if 'S' in g and way != {g['S'][2]}:
                        st['L%d one-way bits not the link\'s' % lv] += 1
                    if lv == 4:
                        has5 = g.get('S', g.get('E'))[1] > 0
                        st['L4 links level 5 has'] += has5
                        if has5 and above is None:
                            st['L4 shapes not naming their level-5 link'] += 1
                        elif above is not None and (par[0] // 4, par[1] // 4, cls, above) not in keys5:
                            st['L4 shapes naming no level-5 link'] += 1
            st['L%d graph links' % lv] += len(ends)
            st['L%d graph links without a shape' % lv] += len(set(ends) - seen)
            for classes in layers.values():
                st['classes in a layer'] += len(set(classes))
                st['class runs'] += 1 + sum(p != q for p, q in zip(classes, classes[1:]))
    n = st['L4 graph links'] + st['L5 graph links']
    loose = sum(st['L%d %s' % (lv, k)] for lv in (4, 5) for k in ('shape links of no graph link', 'graph links without a shape'))
    apart = st['L4 not node to node'] + st['L5 not node to node']
    words = st['L4 shapes not naming their level-5 link'] + st['L4 shapes naming no level-5 link']
    numbered = st['L4 with a route number'] >= 0.4 * st['L4 numbered-class links']
    ways = st['L4 one-way bits not the link\'s'] + st['L5 one-way bits not the link\'s']
    wrong_way = WRONG_WAY[fmt is None or fmt.level_count]
    ok = (loose <= LOOSE * n and apart <= APART * n and words <= NO_LEVEL5_WORD * st['L4 links level 5 has']
          and st['class runs'] <= 2 * st['classes in a layer'] and numbered and ways <= wrong_way * n
          and not st['links of more than %d points' % LINK_POINTS])
    return result('coarse_shapes', ok, st,
                  ['links without a shape or shapes without a link: %d of %d (want %.3f%% or less); not node to node: %d (%.3f%%)' % (loose, n, 100 * LOOSE, apart, 100 * APART),
                   'level-4 shapes without or with a wrong level-5 word: %d of %d (want %.3f%% or less)' % (words, st['L4 links level 5 has'], 100 * NO_LEVEL5_WORD),
                   'class runs %d for %d classes in their layers (want at most two a class)' % (st['class runs'], st['classes in a layer']),
                   'links drawn with more than %d points: %d (want none)' % (LINK_POINTS, st['links of more than %d points' % LINK_POINTS]),
                   'shape links whose one-way bits are not the graph link\'s: %d (want %.3f%% or less)' % (ways, 100 * wrong_way),
                   'level-4 links of classes 3 to 5 with a route number: %d of %d (want 40%% or more)' % (st['L4 with a route number'], st['L4 numbered-class links'])])


def check_coarse_ring_codes(lev4, lev5, coarse):
    """A level-4 or level-5 shape link has code 7 in word 3 where its graph link has 7 in w4,
    and nowhere else. Factory QY8: 699,013 of 699,013 links at level 4, 97,294 of 97,294 at
    level 5. QY7: 498,917 of 498,919 and 48,472 of 48,472."""
    st = collections.Counter()
    bad = []
    for lv, recs in ((4, lev4), (5, lev5)):
        for par, (a, b) in recs.items():
            shape = (coarse or {}).get('codes', {}).get(lv, {}).get(par, {})
            for nd in a['nodes']:
                for e in nd['entries']:
                    key = e['cls'] & 0xf, e['id'] & 0x1fff
                    if e['cls'] & 0x8000 or e['id'] & 0x2000 or key not in shape:
                        continue
                    ring = e['w4'] >> 12 == 7
                    st['L%d links with a shape' % lv] += 1
                    st['L%d ring links' % lv] += ring
                    if ring != (shape[key] == 7):
                        st['L%d ring code in one of the two only' % lv] += 1
                        bad.append((lv, par) + key)
    return result('coarse_ring_codes', not bad, st, ['wrong: %r' % bad[:5]])


NEAR_M = 60               # a level-3 shape this near a level-4 link's middle point is the same road


def check_level3_direction(coarse):
    """A level-3 shape is one-way where the level-4 link at the same place is, and two-way
    where that is. Places with no level-3 shape within NEAR_M, or with both kinds, are left
    out. Factory, whole card: QY8 30,685 of 31,114 one-way and 36,933 of 37,037 two-way; QY7
    27,538 of 27,583 and 30,569 of 30,589. The limits are the QY8 shares, the lower."""
    import math
    st = collections.Counter()
    places = (coarse or {}).get('places', {})
    cell = 0.004
    segs = collections.defaultdict(list)
    for geo, way in places.get(3, ()):
        for i, (a, b) in enumerate(zip(geo, geo[1:])):
            for cx in range(int(min(a[0], b[0]) // cell), int(max(a[0], b[0]) // cell) + 1):
                for cy in range(int(min(a[1], b[1]) // cell), int(max(a[1], b[1]) // cell) + 1):
                    segs[cx, cy].append((a, b, way[i + 1] != 0))
    for p, bits in places.get(4, ()):
        k = 111320 * math.cos(math.radians(p[1]))
        kinds = set()
        cx, cy = int(p[0] // cell), int(p[1] // cell)
        for a, b, oneway in (s for i in (-1, 0, 1) for j in (-1, 0, 1) for s in segs.get((cx + i, cy + j), ())):
            ax, ay, bx, by = (a[0] - p[0]) * k, (a[1] - p[1]) * 111320, (b[0] - p[0]) * k, (b[1] - p[1]) * 111320
            dx, dy = bx - ax, by - ay
            t = max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy))) if dx or dy else 0
            if math.hypot(ax + t * dx, ay + t * dy) <= NEAR_M:
                kinds.add(oneway)
        if len(kinds) == 1:
            st['%s at level 4' % ('one-way' if bits else 'two-way')] += 1
            st['%s at both' % ('one-way' if bits else 'two-way')] += (bits != 0) == kinds.pop()
    ok = (st['one-way at both'] * 31114 >= st['one-way at level 4'] * 30685 and
          st['two-way at both'] * 37037 >= st['two-way at level 4'] * 36933)
    return result('level3_direction', ok, st, ['pass at the factory share: 30,685 of 31,114 one-way and 36,933 of 37,037 two-way'])


GAP = 8                   # level units between two pieces of a road before it counts as a break; at level 1 any gap does
# The limits are none, or the factory's own share on B50R068R where it has some.
NAMED_BREAKS = {1: 0, 2: 3 / 7044, 4: 0, 5: 0}          # roads broken where level 1 names the level's links, or OSM has the way
CLASS_BREAKS = {2: 0, 4: 0, 5: 0}                       # factory 6 of 7,046, 2 of 254, 1 of 91: it picks links one by one, we take whole classes
OPEN_ENDS = {1: 3 / 162392, 2: 0, 4: 4 / 11124, 5: 0}   # shape ends on a parcel line with nothing across
UPPER_CODE = {code: level for level, code in fa_encode.UPPER_LEVEL.items()}

Road = collections.namedtuple('Road', 'label cls points parcel above')    # above: the levels a level-1 shape names a link of


def shape_labels(plain, codec):
    """{word offset: label text} of the names section of a background record."""
    w = struct.unpack_from('>22H', plain)
    sec = plain[4 * w[11]:4 * (w[11] + w[12])] if w[11] != 0xffff else b''
    out, p = {}, 24
    for _ in range(struct.unpack_from('>H', sec, 2)[0] if len(sec) >= 24 else 0):
        if p + 10 > len(sec):
            break
        q = p + 8 + (2 if struct.unpack_from('>H', sec, p + 2)[0] & 0x4000 else 0)
        n = struct.unpack_from('>H', sec, q)[0]
        out[p // 2] = sec[q + 2:q + 2 + n].rstrip(b'\0').decode(codec, 'replace')
        p = q + 2 + n + (n & 1)
    return out


def far_edge(v):
    """2047 is the far parcel line, the next parcel's 0."""
    return v + (v == 2047)


def load_drawn_roads(tile_paths, lev_path, osm=None):
    """{level: dict(scale, roads, parcels, base, long)} of the road shapes drawn at levels 1, 2, 4 and 5.

    Points are in the level's units from longitude -924 and the equator; scale is the degrees
    a unit spans, base the parcel the coarse graph's (0, 0) is. A coarse shape is cut into its
    links. A link of more than LINK_POINTS points is counted in long, not drawn, since the unit
    left all four such links on our Iceland card undrawn. osm is a roads.json to hold level 1
    against."""
    out = {'osm': load_osm_roads(osm, tiles_format(tile_paths))} if osm else {}
    for level in (1, 2):
        per_lon, per_lat = clarion_bg.PARCELS_PER_DEG[level]
        D = out[level] = dict(scale=(1 / (2048 * per_lon), 1 / (2048 * per_lat)), roads=[], parcels=set(), base=(0, 0), long=0)
        for path in tile_paths:
            with TileScan(path) as t:
                cols = collections.defaultdict(dict)
                for r in t.records(section=1, level=level):
                    if not r.sub:
                        cols[r.dir_off, r.slot, r.col][r.k % r.per_col == 0] = r
                for c in cols.values():
                    try:
                        pl = c[True].plain()
                        if len(pl) < 44 or struct.unpack_from('>H', pl, 4)[0] != 0x00fa:
                            continue
                        code, _, shapes = fa_encode.parse_record(pl, level)
                        names = shape_labels(c[False].plain(), t.fmt.codec) if False in c else {}
                    except Exception:
                        continue
                    if not all(S['length'] for S in shapes):
                        continue
                    w = struct.unpack('>%dH' % (len(pl) // 2), pl)
                    row, col = clarion_bg.parcel_rowcol(level, *code)
                    D['parcels'].add((row, col))
                    for S in shapes:
                        pts = [(col * 2048 + far_edge(w[i] & 0x7ff), row * 2048 + far_edge(w[i + 1] & 0x7ff)) for i in S['points']]
                        q = S['at'] + 6 + (w[S['at'] + 3] & 3)
                        above = {UPPER_CODE.get(u & 0xe000) for u in w[q:q + (w[S['at']] >> 13)]}
                        D['roads'].append(Road(names.get(S['name'], ''), S['cls'], pts, (row, col), above))
    if lev_path is None:
        return out
    data = lev_path if isinstance(lev_path, (bytes, bytearray)) else open(lev_path, 'rb').read()
    lev = lev_file.read(data, decode=True)
    for layer in lev['sections'][0]['levels']:
        if layer['level'] not in (4, 5):
            continue
        D = out[layer['level']] = dict(scale=None, roads=[], parcels=set(), base=(0, 0), long=0)
        for d in layer['dirs']:
            per = d['pairs'] // d['cols']
            lon0, lat0, pw, ph = clarion_bg.parcel_geo(layer['level'], d['y'], (d['x'] + 1024) & 0xffff, 0, 0)
            D['scale'], D['base'] = (pw / 2048, ph / 2048), (round(lat0 / ph), round((lon0 + 924) / pw))
            for row, rr in enumerate(d['rows'] or []):
                for col in range(d['cols'] if rr else 0):
                    r, bg = rr[col * per], rr[col * per + per - 1]
                    if r is None or 'plain' not in r or len(r['plain']) < 44:
                        continue
                    w = struct.unpack('>%dH' % (len(r['plain']) // 2), r['plain'])
                    if w[2] != 0x00fa or w[7] in (0, 0xffff):
                        continue
                    try:
                        shapes = list(coarse_walk(w[2 * w[7]:2 * (w[7] + w[8])]))
                        names = shape_labels(bg['plain'], lev['fmt'].codec) if bg and 'plain' in bg else {}
                    except Exception:
                        continue
                    pc = (row + D['base'][0], col + D['base'][1])
                    D['parcels'].add(pc)
                    for _, cls, name, above, ids, pts, way, _ in shapes:
                        pts = [(pc[1] * 2048 + far_edge(x), pc[0] * 2048 + far_edge(y)) for x, y in pts]
                        cuts = [at for lid, at in ids] or [0]
                        for a, b in zip(cuts, cuts[1:] + [len(pts) - 1]):
                            if b - a + 1 > LINK_POINTS:
                                D['long'] += 1
                            else:
                                D['roads'].append(Road(names.get(name, ''), cls, pts[a:b + 1], pc, ()))
    return out


def load_osm_roads(path, fmt=None):
    """The ways of a roads.json as a level for road_breaks, in level-1 units, labelled as
    rd_build labels them."""
    per_lon, per_lat = clarion_bg.PARCELS_PER_DEG[1]
    text = (lambda t: fmt.text(t).decode(fmt.codec, 'replace')) if fmt else str
    with open(path) as f:
        ways = json.load(f)
    roads = [Road(text('2=' + w['ref'] if w.get('ref') else '1=' + (w.get('name') or '')), None,
                  [(round((lon + 924) * per_lon * 2048), round(lat * per_lat * 2048)) for lon, lat in w['nodes']], None, ()) for w in ways]
    return dict(scale=(1 / (2048 * per_lon), 1 / (2048 * per_lat)), roads=roads, parcels=set(), base=(0, 0), long=0)


def road_key(label):
    """What names a road across shapes and levels: its route number, or else its name."""
    parts = dict(p.split('=', 1) for p in label.split(';') if '=' in p)
    return '2=' + parts['2'] if parts.get('2') else '1=' + parts['1'].upper() if parts.get('1') else None


class Joined:
    """Sets of things joined pair by pair."""

    def __init__(self):
        self.up = {}

    def find(self, a):
        up = self.up
        while up.setdefault(a, a) != a:
            up[a] = a = up[up[a]]
        return a

    def join(self, a, b):
        self.up[self.find(a)] = self.find(b)


def road_pieces(roads):
    """{road key: [points of one piece]}: shapes of a road that share a point are one piece."""
    sets = Joined()
    for r in roads:
        k = road_key(r.label)
        if k:
            for a in r.points:
                sets.join((k, r.points[0]), (k, a))
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for k, pt in list(sets.up):
        out[k][sets.find((k, pt))].append(pt)
    return {k: list(v.values()) for k, v in out.items()}


def road_breaks(D, ref, tol=2):
    """(roads both have, [(gap in units, road key, point, point)]) of roads drawn in pieces the reference joins.

    The reference joins its own pieces only where they share a point. One of our points within
    tol of two of them goes to the nearest, so a gap the reference has is not ours: the Finland
    card had 32 places where two OSM ways of one name end a unit or two apart."""
    mine, below = road_pieces(D['roads']), road_pieces(ref['roads'])
    fx, fy = D['scale'][0] / ref['scale'][0], D['scale'][1] / ref['scale'][1]
    breaks = []
    for k, ps in mine.items():
        if len(ps) < 2 or k not in below:
            continue
        at = collections.defaultdict(set)
        for n, pts in enumerate(below[k]):
            for x, y in pts:
                at[int(x / fx), int(y / fy)].add(n)
        sets = Joined()
        for n, pts in enumerate(ps):
            for x, y in pts:
                near = {}
                for dx in range(-tol, tol + 1):
                    for dy in range(-tol, tol + 1):
                        for m in at.get((x + dx, y + dy), ()):
                            near[m] = min(near.get(m, tol + 1), max(abs(dx), abs(dy)))
                for m, d in near.items():
                    if d == min(near.values()): sets.join(('piece', n), ('below', m))
        groups = collections.defaultdict(list)
        for n in range(len(ps)):
            if ('piece', n) in sets.up:
                groups[sets.find(('piece', n))].append(n)
        for g in groups.values():
            g.sort(key=lambda n: -len(ps[n]))
            for n in g[1:]:
                rest = [q for o in g if o != n for q in ps[o]]
                breaks.append(min((max(abs(p[0] - q[0]), abs(p[1] - q[1])), k, p, q) for p in ps[n] for q in rest))
    return len(set(mine) & set(below)), sorted(breaks, reverse=True)


def open_ends(D, dead_ends=(), tol=2):
    """(shape ends on a parcel line, [(label, point)] of those with nothing across).

    An end another shape shares, one whose far parcel is not loaded and one on a graph node
    with no link across are left out: a junction or a dead end the grid rounded onto the line."""
    near, ends = collections.defaultdict(set), collections.Counter()
    for r in D['roads']:
        ends.update({r.points[0], r.points[-1]})
        for a in r.points:
            if min(a[0] % 2048, -a[0] % 2048, a[1] % 2048, -a[1] % 2048) <= tol:
                near[a].add(r.parcel)
    n, bad = 0, []
    for label, cls, pts, pc, above in D['roads']:
        for a in {pts[0], pts[-1]}:
            if a[0] % 2048 and a[1] % 2048:
                continue
            n += 1
            across = {((a[1] - dy) // 2048, (a[0] - dx) // 2048) for dx in (0, 1) for dy in (0, 1)} - {pc}
            met = any(near.get((a[0] + dx, a[1] + dy), set()) - {pc} for dx in range(-tol, tol + 1) for dy in range(-tol, tol + 1))
            if not met and across & D['parcels'] and ends[a] == 1 and a not in dead_ends:
                bad.append((label, a))
    return n, bad


def graph_dead_ends(recs, base=(0, 0)):
    """Where a graph node has no link to the next parcel, in the units of load_drawn_roads."""
    return {((pc[1] + base[1]) * 2048 + far_edge(nd['x']), (pc[0] + base[0]) * 2048 + far_edge(nd['y']))
            for pc, a in recs.items() for nd in a['nodes'] if not any(e['cls'] & 0x8000 for e in nd['entries'])}


def check_road_continuity(drawn, L1, L2, lev4, lev5):
    """A road is drawn whole at every level.

    A shape end on a parcel line needs a shape across it. At levels 2, 4 and 5 a road may fall
    into pieces only where level 1 has it in pieces, read both from the level-1 shapes that
    name a link of the level and from those of the classes it carries. Level 1 is held against
    the OSM ways the same way when they are given. At level 1 any gap is a break: each one on
    our Iceland card was a road cut in the graph beside a parcel corner. Above it a gap of GAP
    units passes, as 1,711 of the 1,725 gaps on factory B50R068R are that narrow (a slip road
    or a crossing left out). By name the factory has no wider break in 223 roads at level 4 or
    49 at level 5; its other shares are in the limits above."""
    st, notes, ok = collections.Counter(), [], True
    if not drawn or not drawn[1]['roads']:
        return result('road_continuity', True, st, ['no road shapes loaded, nothing to check'])
    graphs = {1: L1, 2: {pc: a for pc, (a, b) in L2.items()}, 4: {pc: a for pc, (a, b) in lev4.items()},
              5: {pc: a for pc, (a, b) in lev5.items()}}

    def where(level, p):
        sx, sy = drawn[level]['scale']
        return '%.4f,%.4f' % (p[1] * sy, p[0] * sx - 924)

    for level in (1, 2, 4, 5):
        D = drawn.get(level)
        if not D or not D['roads']:
            continue
        ends, bad = open_ends(D, graph_dead_ends(graphs[level], D['base']))
        st['L%d ends on a parcel line' % level], st['L%d with nothing across' % level] = ends, len(bad)
        st['L%d links too long to draw' % level] = D['long']
        notes += ['level %d: %s ends on a parcel line at %s with no shape across' % (level, label or 'an unnamed road', where(level, a))
                  for label, a in bad[:20]]
        ok = ok and len(bad) <= OPEN_ENDS[level] * ends
        classes = {r.cls for r in D['roads']}
        refs = ([('against OSM', 'OSM has whole', drawn['osm']['roads'], NAMED_BREAKS[1])] if 'osm' in drawn else []) if level == 1 else [
            ('by name', 'level 1 names whole', [r for r in drawn[1]['roads'] if level in r.above], NAMED_BREAKS[level]),
            ('by class', 'level 1 has whole in these classes', [r for r in drawn[1]['roads'] if r.cls in classes], CLASS_BREAKS[level])]
        told = set()
        for tag, what, roads, share in refs:
            n, breaks = road_breaks(D, dict(drawn[1], roads=roads))
            wide = [b for b in breaks if b[0] > GAP * (level > 1)]
            for key, v in (('roads', n), ('gaps', len(breaks)), ('breaks', len(wide))):
                st['L%d %s %s' % (level, key, tag)] = v
            notes += ['level %d: %s is broken between %s and %s (%d units), a road %s' % (level, k, where(level, p), where(level, q), gap, what)
                      for gap, k, p, q in wide[:20] if (k, p, q) not in told]
            told.update((k, p, q) for gap, k, p, q in wide)
            ok = ok and len(wide) <= share * n
    notes.append('a break is a gap of more than %d units, at level 1 any gap; pass at none, or the factory share where it has some: '
                 '3 of 7,044 roads by name at level 2, 3 of 162,392 ends with nothing across at level 1 and 4 of 11,124 at level 4' % GAP)
    return result('road_continuity', ok, st, notes)


def run_all(L1, L2, SH1, lev4, lev5, origin=None, coarse=None, fmt=None, drawn=None, SH2=None):
    out = []
    out.append(check_expansion(L1, L2))
    out.append(check_l2_triple_w4(L1, L2, fmt))
    out.append(check_edges(L1, L2, lev4, lev5))
    out.append(check_l2_edge_triples(L2))
    out.append(check_l2_block_order(L2))
    out.append(check_no_junction_on_parcel_line(L1, L2))
    out.append(check_node_order(lev4, lev5, L1, L2))
    out.append(check_l1_link_direction(L1, L2))
    out.append(check_l2_nflags(L2, fmt))
    out.append(check_roundabouts(L1, L2, SH1, fmt))
    out.append(check_shape_link_codes(L1, L2, SH1, SH2))
    out.append(check_graph_words(L1, L2, lev4, lev5, fmt))
    out.append(check_upper_link_shapes(L1, L2, SH1))
    out.append(check_l2_nodes_on_l1(L1, L2))
    out.append(check_traffic_shapes(SH1))
    out.append(check_coarse_link_words(L1, L2, SH1, lev4, lev5, origin))
    out.append(check_hierarchy_codes(L1, SH1, fmt))
    out.append(check_length_time_units(L1, L2))
    out.append(check_coarse_graph_present(L2, lev4, lev5))
    out.append(check_coarse_shapes(lev4, lev5, coarse, fmt))
    out.append(check_coarse_ring_codes(lev4, lev5, coarse))
    out.append(check_level3_direction(coarse))
    out.append(check_road_continuity(drawn, L1, L2, lev4, lev5))
    return out


def check_tiles(expected, per_tile, errors):
    """Every expected road cell is on the card with L1 and L2 records, and every record
    parses. The ten rules pass on nothing, so this is the one that fails an empty card."""
    st = collections.Counter(tiles=len(per_tile), L1=sum(n[0] for n in per_tile.values()),
                             L2=sum(n[1] for n in per_tile.values()))
    notes = []
    for c in expected:
        if c not in per_tile:
            notes.append('%s: not on the card' % c)
        elif not per_tile[c][0] or not per_tile[c][1]:
            notes.append('%s: L1 %d, L2 %d records' % (c, per_tile[c][0], per_tile[c][1]))
    if not st['L1'] or not st['L2']:
        notes.append('no road records on the card at all')
    for name, n in sorted(errors.items()):
        st['parse_errors'] += n
        notes.append('%s: %d records do not parse' % (name, n))
    return result('tiles', not notes, st, notes)


# Card extraction (FAT, read-only) and the whole-card / field-level diff.

def extract_card(img):
    """{'MAPAL001/NAME.DAT': bytes, ...} plus 'LEV3456R.DAT': bytes, read off a card image."""
    import fatread
    fs = fatread.Fat32(img)
    out = {}
    for path, size, clus, mt in fs.walk():
        if size is None:
            continue
        up = path.upper()
        if up.startswith('/MAPAL001/') and up.endswith('.DAT') or up.endswith('/LEV3456R.DAT'):
            out[path.lstrip('/')] = fs.read(path)
    fs.close()
    return out


def whole_card_diff(img_a, img_b):
    """(only_in_a, only_in_b, [(path, size_a, size_b, md5_a, md5_b)] differing, common_count),
    like imgcmp. common_count is len(files in both cards), so identical = common_count - len(diff)."""
    import fatread

    def take(p):
        fs = fatread.Fat32(p)
        out = {}
        for path, size, clus, mt in fs.walk():
            out[path] = (size, None if size is None else fs.md5(clus, size))
        fs.close()
        return out
    a, b = take(img_a), take(img_b)
    ka, kb = set(a), set(b)
    diff = [(k, a[k][0], b[k][0], a[k][1], b[k][1]) for k in sorted(ka & kb) if a[k] != b[k]]
    return sorted(ka - kb), sorted(kb - ka), diff, len(ka & kb)


def section_sizes(path):
    """{(section, level): (records, plain_bytes, raw_bytes)} over every section/level a tile
    has, not just section 2 - a whole-file size change with no section-2 field change (a
    compression-size effect of changed bits, or a change outside the ten rules' own fields)
    needs this to find which section actually grew, instead of staying unexplained."""
    out = {}
    with TileScan(path) as t:
        for sec in range(1, 5):
            for lvl in range(1, 6):
                recs = list(t.records(section=sec, level=lvl))
                if recs:
                    out[(sec, lvl)] = (len(recs), sum(len(r.plain()) for r in recs),
                                        sum(len(r.raw()) for r in recs))
    return out


def section_size_diff(name, path_a, path_b):
    """[(label, differs, (recs_a, plain_a, raw_a), (recs_b, plain_b, raw_b))] for every
    (section, level) present in either tile, in section/level order."""
    a, b = section_sizes(path_a), section_sizes(path_b)
    rows = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        rows.append(('%s sec%d lvl%d' % (name, key[0], key[1]), va != vb, va, vb))
    return rows


ENTRY_FIELDS = ('w4', 'w5', 'id', 'cls', 'a', 'b')
NODE_FIELDS = ('ncls', 'nflags', 'x', 'y')


def field_diff(name, recs_a, recs_b, level):
    """[(field, changed_count, consistent_bit_note)] between two {pc: record-or-(a,b)} maps
    for one tile and level, entries/nodes compared positionally per matching parcel."""
    out = []
    node_changed = collections.Counter()
    entry_changed = collections.Counter()
    entry_bit_xor = collections.Counter()
    node_bit_xor = collections.Counter()
    common = set(recs_a) & set(recs_b)
    for pc in common:
        ra, rb = recs_a[pc], recs_b[pc]
        a = ra[0] if isinstance(ra, tuple) else ra
        b = rb[0] if isinstance(rb, tuple) else rb
        for na, nb in zip(a['nodes'], b['nodes']):
            for f in NODE_FIELDS:
                if na[f] != nb[f]:
                    node_changed[f] += 1
                    node_bit_xor[f] |= (na[f] ^ nb[f])
            for ea, eb in zip(na['entries'], nb['entries']):
                for f in ENTRY_FIELDS:
                    if ea[f] != eb[f]:
                        entry_changed[f] += 1
                        entry_bit_xor[f] |= (ea[f] ^ eb[f])
    for f, n in sorted(node_changed.items()):
        out.append(('%s L%d node.%s' % (name, level, f), n, node_bit_xor[f]))
    for f, n in sorted(entry_changed.items()):
        out.append(('%s L%d entry.%s' % (name, level, f), n, entry_bit_xor[f]))
    return out


def bit_note(xor):
    if xor == 0:
        return ''
    bits = [hex(1 << i) for i in range(16) if xor & (1 << i)]
    return ' (bit%s %s set)' % ('s' if len(bits) > 1 else '', '/'.join(bits))


# CLI

def load_for_check(tile_paths, lev_path):
    L1, L2, SH1 = load_road_tiles(tile_paths)
    lev = load_lev(lev_path)
    return L1, L2, SH1, lev.get(4, {}), lev.get(5, {}), lev.get('origin')


def print_report(title, checks):
    print('== %s ==' % title)
    for c in checks:
        mark = 'PASS' if c['passed'] else 'FAIL'
        print('[%s] %s  %s' % (mark, c['name'], ' '.join('%s=%s' % kv for kv in c['counts'].items())))
        for n in c['notes']:
            print('    ' + str(n))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--card')
    ap.add_argument('--prev')
    ap.add_argument('--tiles', nargs='*', default=[])
    ap.add_argument('--lev')
    ap.add_argument('--roads', help='the roads.json the tiles were built from: level 1 is then held against the OSM ways')
    ap.add_argument('--factory', nargs='*', default=[])
    ap.add_argument('--factory-lev')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--cells', default='', help='road cells the card must carry, comma-separated')
    args = ap.parse_args(argv)

    report = {}

    if args.card:
        files = extract_card(args.card)
        tiles = {k: v for k, v in files.items() if k.upper().startswith('MAPAL001/') and k.upper() != 'MAPAL001/LEV3456R.DAT'}
        lev = files.get('MAPAL001/LEV3456R.DAT') or next((v for k, v in files.items() if k.upper().endswith('LEV3456R.DAT')), None)
        L1, L2, SH1 = {}, {}, {}
        errors, per_tile = collections.Counter(), {}
        for name in sorted(tiles):
            e = collections.Counter()
            l1, l2, sh1 = load_road_tiles([tiles[name]], e)
            cell = os.path.basename(name)[:-4]
            per_tile[cell] = (len(l1), len(l2))
            errors.update({'%s %s' % (cell, k): n for k, n in e.items()})
            L1.update(l1); L2.update(l2); SH1.update(sh1)
        levd = load_lev(lev)
        checks = run_all(L1, L2, SH1, levd.get(4, {}), levd.get(5, {}), levd.get('origin'), load_lev_shapes(lev),
                         tiles_format(tiles[k] for k in sorted(tiles)), load_drawn_roads([tiles[k] for k in sorted(tiles)], lev, args.roads),
                         load_l2_shapes(tiles[k] for k in sorted(tiles)))
        checks.append(check_tiles([c for c in args.cells.split(',') if c], per_tile, errors))
    else:
        errors = collections.Counter()
        L1, L2, SH1 = load_road_tiles(args.tiles, errors)
        levd = load_lev(args.lev)
        checks = run_all(L1, L2, SH1, levd.get(4, {}), levd.get(5, {}), levd.get('origin'), load_lev_shapes(args.lev),
                         tiles_format(args.tiles), load_drawn_roads(args.tiles, args.lev, args.roads), load_l2_shapes(args.tiles))
        checks.append(check_tiles([], {'tiles': (len(L1), len(L2))}, errors))

    print_report(os.path.basename(args.card or 'tiles'), checks)
    report['card'] = dict(title=args.card or 'tiles', checks=checks)

    if args.factory:
        L1f, L2f, SH1f = load_road_tiles(args.factory)
        levf = load_lev(args.factory_lev)
        fchecks = run_all(L1f, L2f, SH1f, levf.get(4, {}), levf.get(5, {}), levf.get('origin'), load_lev_shapes(args.factory_lev),
                          tiles_format(args.factory), load_drawn_roads(args.factory, args.factory_lev), load_l2_shapes(args.factory))
        print_report('factory (%s)' % ', '.join(os.path.basename(f) for f in args.factory), fchecks)
        report['factory'] = dict(checks=fchecks)

    if args.prev and args.card:
        print('== diff: whole card, %s -> %s ==' % (args.prev, args.card))
        only_a, only_b, diff, common_count = whole_card_diff(args.card, args.prev)
        print('only in %s: %r' % (args.card, only_a))
        print('only in %s: %r' % (args.prev, only_b))
        for path, sa, sb, ma, mb in diff:
            print('  DIFFERS %-26s size %s -> %s  md5 %s -> %s' % (path, sb, sa, mb, ma))

        # identical here is whole-card: common_count minus the DIFFERS above, not scoped to
        # extract_card()'s MAPAL001/LEV3456R-only subset (that mismatch used to make this
        # number meaningless - e.g. 5 road/lev files in common, 4 REFER001 files differing,
        # reported as "1 identical" with no road tile involved in either number)
        print('identical: %d files' % (common_count - len(diff)))
        prev_files = extract_card(args.prev)
        prev_tiles = {k: v for k, v in prev_files.items() if k.upper().startswith('MAPAL001/') and 'LEV3456R' not in k.upper()}
        common = set(tiles) & set(prev_tiles)
        field_rows = []
        for name in sorted(common):
            L1a, L2a, _ = load_road_tiles([tiles[name]])
            L1b, L2b, _ = load_road_tiles([prev_tiles[name]])
            field_rows += field_diff(os.path.basename(name), L1a, L1b, 1)
            field_rows += field_diff(os.path.basename(name), L2nodes(L2a), L2nodes(L2b), 2)
        print('== diff: field-level, by tile and level ==')
        for label, n, xor in field_rows:
            print('%s: %d changed%s' % (label, n, bit_note(xor)))

        # Field-level diff above only decodes section 2 (the road graph). A tile's whole-card
        # size can change through section 1 (background/shapes), section 4 (signposts) or
        # compression alone (same bits, different entropy) with no section-2 field touched -
        # that's a size change the field diff would otherwise hide. This checks every
        # section/level's record count and both plain and raw (compressed) byte totals.
        size_rows = []
        for name in sorted(common):
            size_rows += section_size_diff(os.path.basename(name), tiles[name], prev_tiles[name])
        print('== diff: section sizes, by tile, section and level ==')
        for label, differs, va, vb in size_rows:
            mark = '  <-- DIFFERS' if differs else ''
            print('%s: card(recs,plain,raw)=%s prev=%s%s' % (label, va, vb, mark))

        report['diff'] = dict(only_in_card=only_a, only_in_prev=only_b,
                              files_differing=[d[0] for d in diff],
                              fields=[dict(field=l, changed=n, xor=xor) for l, n, xor in field_rows],
                              section_sizes=[dict(label=l, differs=d, card=va, prev=vb)
                                             for l, d, va, vb in size_rows])

    if args.json:
        print(json.dumps(report, indent=2, default=str))

    return 0 if all(c['passed'] for c in checks) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
