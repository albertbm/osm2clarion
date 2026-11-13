#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a built card's road graph and coarse routing file against every format rule
measured off factory tiles, and diff a card against the previous one field by field.

    card_check.py --card A.img [--cells C,C...] [--prev B.img] [--factory TILE ...] [--json]
    card_check.py --tiles TILE ... [--lev LEV3456R.DAT] [--factory TILE ...] [--json]

With --card, MAPAL001/*.DAT and LEV3456R.DAT are read straight off the FAT image (no
mount, no full-image load). With --tiles, the files are given directly - useful for a
quick check against a factory tile with no card to extract from.

Each rule below started as a one-off measurement script, checked against factory
tiles and a built card before being folded in here. A rule that fails on factory
data is a rule bug, not a card bug - run --factory and
read the factory column before trusting a FAIL on the card under test.

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
    """{level: {(row, col): [(layer, class, links, level-5 word or None, named)]}} of the
    level-4 and level-5 road shapes, walked as the unit walks them (Navi.exe 0x516604), a
    link as (id, first point, last point, y bits, points), or {} when path is None."""
    if path is None:
        return {}
    data = path if isinstance(path, (bytes, bytearray)) else open(path, 'rb').read()
    out = {}
    for layer in lev_file.read(data, decode=True)['sections'][0]['levels']:
        if layer['level'] not in (4, 5):
            continue
        recs = out.setdefault(layer['level'], {})
        for d in layer['dirs']:
            per = d['pairs'] // d['cols']
            for row, rr in enumerate(d['rows'] or []):
                for col in range(d['cols'] if rr else 0):
                    r = rr[col * per]
                    if r is None or 'plain' not in r or len(r['plain']) < 44:
                        continue
                    w = struct.unpack('>%dH' % (len(r['plain']) // 2), r['plain'])
                    if w[2] != 0x00fa or w[7] in (0, 0xffff):
                        continue
                    try:
                        recs[row, col] = list(coarse_shapes(w[2 * w[7]:2 * (w[7] + w[8])]))
                    except IndexError:
                        recs[row, col] = []
    return out


def coarse_shapes(u):
    """The shapes of one coarse road unit, layer by layer."""
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
            links = [(lid, pts[at], pts[nxt[1] if nxt else -1], set(way[at + 1:nxt[1] + 1 if nxt else None]),
                      (nxt[1] if nxt else len(pts) - 1) - at + 1) for (lid, at), nxt in zip(ids, ids[1:] + [None])]
            yield layer, w2 >> 12, links, above[0] & 0x1fff if above else None, name != 0xffff
            p += w0 & 0x1fff


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


def check_expansion(L1, L2):
    """Every L2 link unfolds into a continuous, same-class L1 path of about the same
    length. Factory is not 100%: a through-node/ambiguous class of link is accepted
    on measured card data, so the threshold is a ratio, not zero."""
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
    ok = total == 0 or bad / total <= 0.03
    return result('expansion', ok, st, [
        'bad/total = %d/%d (%.2f%%), pass threshold <= 3%%' % (bad, total, 100 * bad / total if total else 0)])


def check_l2_triple_w4(L1, L2):
    """L2 part-B link triples: w4 is a real entry of the L1 child the triple names
    Factory is 100%."""
    st = collections.Counter()
    bad = []
    for pc, (a, b) in L2.items():
        for x in b['nodes']:
            k = x['upper'] & 0x3fff
            own1 = L1.get((pc[0] * 4 + (x['parcel'] >> 14), pc[1] * 4 + (x['parcel'] >> 6 & 3)))
            cn = own1['nodes'][x['child']] if own1 and x['child'] < len(own1['nodes']) else None
            if k >= len(a['nodes']):
                continue
            for e, (w0, w2, w4) in zip(a['nodes'][k]['entries'], x['entries']):
                if e['cls'] & 0x8000:
                    continue
                st['link'] += 1
                st['w2==own cell'] += w2 == x['parcel']
                if cn is None:
                    st['child unresolved'] += 1
                    bad.append((pc, k))
                    continue
                ok = any((z['other'] & 0x3fff) == w4 for z in cn['entries'])
                st['w4 adjacent to own L1 child'] += ok
                if not ok:
                    bad.append((pc, k, x['child'], w4))
    passed = st['link'] == 0 or (st['w4 adjacent to own L1 child'] == st['link'] and
                                  st['w2==own cell'] == st['link'])
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


def check_node_order(lev4, lev5):
    """Nodes within an L4/L5 record are ordered by class, then x, then y.
    Measured on factory at 509/513 (L4) and 42/42 (L5): close to exact, but no simple
    key fits every within-class case, so this is a >=95% threshold, not exact -
    and it is NOT applied to L1/L2, where a direct test here showed the x,y part of the
    key does not hold (84% on one factory L2 tile); only the LEV-level claim is measured."""
    st = collections.Counter()
    bad = []
    for level, recs in ((4, {pc: a for pc, (a, b) in lev4.items()}),
                        (5, {pc: a for pc, (a, b) in lev5.items()})):
        for pc, a in recs.items():
            keys = [(n['ncls'], n['x'], n['y']) for n in a['nodes']]
            st['%d:records' % level] += 1
            if keys == sorted(keys):
                st['%d:ordered' % level] += 1
            elif len(bad) < 10:
                bad.append((level, pc))
    total = sum(v for k, v in st.items() if k.endswith('records'))
    ordered = sum(v for k, v in st.items() if k.endswith('ordered'))
    return result('node_order', total == 0 or ordered / total >= 0.95, st, bad)


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


def check_l2_nflags(L2):
    """L2 nodes carry the hierarchy flags 0x4000/0x2000 (lev_graph.patch_l2_flags). Report
    counts; fail only if the tile has L2 nodes at all but neither flag ever appears - the
    bug that hit card 075."""
    st = collections.Counter()
    for pc, a in L2nodes(L2).items():
        for n in a['nodes']:
            st['nodes'] += 1
            st['nflags 0x4000'] += bool(n['nflags'] & 0x4000)
            st['nflags 0x2000'] += bool(n['nflags'] & 0x2000)
            st['nflags other bits'] += bool(n['nflags'] & ~0x6000)
    ok = st['nodes'] == 0 or st['nflags 0x4000'] + st['nflags 0x2000'] > 0
    return result('l2_nflags', ok, st)


def check_roundabouts(L1, L2, SH1):
    """Roundabout ring entries carry w4 & 0x7000 == 0x7000 at L1 and L2, and a section-1
    ring string's header word 3 (fa_encode.shape_words: cls, ring-flag, name, ...) is
    0x7004. Not a clean 100%% invariant even on factory - Koln and a UK tile disagree
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
            flagged = hdr3 == 0x7004
            st['1:section-1 shapes'] += 1
            st['1:section-1 ring strings (hdr3==0x7004)'] += flagged
            if flagged:
                ends = sh['ends']
                both_ring = all((p in {(x, y) for pc2, x, y in ring_nodes[1]}) for pair in ends for p in pair)
                st['1:ring strings with ring-flagged entries at both ends'] += both_ring
    # the w4&0x7000 mask and the header-word-3 flag should agree on every ring string this
    # writer produced; whether the factory's OWN two fields agree is unsettled (Koln and
    # a UK tile disagree), so this only gates on our own internal consistency
    total_rings = st['1:section-1 ring strings (hdr3==0x7004)']
    consistent = st['1:ring strings with ring-flagged entries at both ends']
    ok = total_rings == 0 or consistent == total_rings
    return result('roundabouts', ok, st)


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


def walk_shapes(shapes, first):
    """Follow the shapes naming one upper link from the level-1 node under its first node,
    the way the unit does: the shape that starts where the last one ended (Navi.exe 0x34d2b8)."""
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
    return 'backwards' if any(e == pos for s, e in left) else 'walk stops short'


def first_ends(a):
    """[(class, link id, node index)] of the first node of each link of one graph record."""
    return [(e['cls'] & 0xf, e['id'] & 0x1fff, k) for k, nd in enumerate(a['nodes']) for e in nd['entries']
            if not e['cls'] & 0x8000 and not e['id'] & 0x2000]


def check_upper_link_shapes(L1, L2, SH1):
    """The level-1 shapes naming a level-2 link run the way the link runs: the unit takes
    the shape that starts where the last one ended (Navi.exe 0x34d2b8), so one drawn
    backwards ends its walk. Factory B50R068R: none backwards on 46,835 links."""
    named = upper_shapes(SH1)
    st = collections.Counter()
    for pc2, (a, b) in L2.items():
        blocks = blocks_of(b)
        for cls, lid, k in first_ends(a):
            node = l1_node(L1, pc2, blocks.get(k))
            if (pc2[0], pc2[1], cls, lid) in named and node:
                st['links named'] += 1
                st[walk_shapes(named[pc2[0], pc2[1], cls, lid], node)] += 1
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


def check_hierarchy_codes(L1, SH1):
    """Bits 1-2 of a level-1 link's w4 count the levels above that have the link: the number
    of upper-link words in its shape header. Factory B50R068R: 2,192,458 of 2,192,458 entries."""
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
                st['code is not the word count'] += (e['w4'] >> 1 & 3) != n
    return result('hierarchy_codes', st['code is not the word count'] <= 0.005 * st['entries'], st,
                  ['link entries whose w4 code is not their shape\'s word count: %d (factory 0, pass at 0.5%%)' % st['code is not the word count']])


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

    base = (origin[0] * 32, origin[1] * 32)
    for lv, span, recs in ((4, 64, lev4), (5, 256, lev5)):
        named = upper_shapes(SH1, {4: 0x6000, 5: 0x8000}[lv], span, base)
        links = {par + (cls, lid): k for par, (a, b) in recs.items() for cls, lid, k in first_ends(a)}
        st['L%d names of no link' % lv] = sum(key[:2] in recs and key not in links for key in named)
        for key, k in links.items():
            node = under(lv, key[:2], k)
            if node is None:
                continue          # not under the loaded tiles
            st['L%d links' % lv] += 1
            if key in named:
                st['L%d %s' % (lv, walk_shapes(named[key], node))] += 1
            else:
                st['L%d not named' % lv] += 1
    n = st['L4 links'] + st['L5 links']
    ok = (st['words out of order'] <= 0.001 * st['shapes with words']
          and st['L4 names of no link'] + st['L5 names of no link'] <= 0.02 * n
          and st['L4 backwards'] + st['L5 backwards'] <= 0.005 * n
          and st['L4 not named'] <= 0.15 * st['L4 links'])
    return result('coarse_link_words', ok, st,
                  ['level-4 links no level-1 shape names: %d of %d (want 15%% or less); coarse links with a shape drawn backwards: %d (want 0.5%% or less)'
                   % (st['L4 not named'], st['L4 links'], st['L4 backwards'] + st['L5 backwards'])])


LINK_POINTS = 32          # the most one coarse link has on the factory card


def check_coarse_shapes(lev4, lev5, coarse):
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
    ok = (loose <= 0.002 * n and apart <= 0.005 * n and words <= 0.005 * max(1, st['L4 links level 5 has'])
          and st['class runs'] <= 2 * st['classes in a layer'] and numbered and ways <= 0.02 * n
          and not st['links of more than %d points' % LINK_POINTS])
    return result('coarse_shapes', ok, st,
                  ['links without a shape or shapes without a link: %d of %d (want 0.2%% or less); not node to node: %d (0.5%%)' % (loose, n, apart),
                   'level-4 shapes without or with a wrong level-5 word: %d of %d (want 0.5%% or less)' % (words, st['L4 links level 5 has']),
                   'class runs %d for %d classes in their layers (want at most two a class)' % (st['class runs'], st['classes in a layer']),
                   'links drawn with more than %d points: %d (want none)' % (LINK_POINTS, st['links of more than %d points' % LINK_POINTS]),
                   'shape links whose one-way bits are not the graph link\'s: %d (want 2%% or less)' % ways,
                   'level-4 links of classes 3 to 5 with a route number: %d of %d (want 40%% or more)' % (st['L4 with a route number'], st['L4 numbered-class links'])])


def run_all(L1, L2, SH1, lev4, lev5, origin=None, coarse=None):
    out = []
    out.append(check_expansion(L1, L2))
    out.append(check_l2_triple_w4(L1, L2))
    out.append(check_edges(L1, L2, lev4, lev5))
    out.append(check_no_junction_on_parcel_line(L1, L2))
    out.append(check_node_order(lev4, lev5))
    out.append(check_l1_link_direction(L1, L2))
    out.append(check_l2_nflags(L2))
    out.append(check_roundabouts(L1, L2, SH1))
    out.append(check_upper_link_shapes(L1, L2, SH1))
    out.append(check_l2_nodes_on_l1(L1, L2))
    out.append(check_traffic_shapes(SH1))
    out.append(check_coarse_link_words(L1, L2, SH1, lev4, lev5, origin))
    out.append(check_hierarchy_codes(L1, SH1))
    out.append(check_length_time_units(L1, L2))
    out.append(check_coarse_graph_present(L2, lev4, lev5))
    out.append(check_coarse_shapes(lev4, lev5, coarse))
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
        checks = run_all(L1, L2, SH1, levd.get(4, {}), levd.get(5, {}), levd.get('origin'), load_lev_shapes(lev))
        checks.append(check_tiles([c for c in args.cells.split(',') if c], per_tile, errors))
    else:
        errors = collections.Counter()
        L1, L2, SH1 = load_road_tiles(args.tiles, errors)
        levd = load_lev(args.lev)
        checks = run_all(L1, L2, SH1, levd.get(4, {}), levd.get(5, {}), levd.get('origin'), load_lev_shapes(args.lev))
        checks.append(check_tiles([], {'tiles': (len(L1), len(L2))}, errors))

    print_report(os.path.basename(args.card or 'tiles'), checks)
    report['card'] = dict(title=args.card or 'tiles', checks=checks)

    if args.factory:
        L1f, L2f, SH1f = load_road_tiles(args.factory)
        levf = load_lev(args.factory_lev)
        fchecks = run_all(L1f, L2f, SH1f, levf.get(4, {}), levf.get(5, {}), levf.get('origin'), load_lev_shapes(args.factory_lev))
        print_report('factory (%s)' % ', '.join(os.path.basename(f) for f in args.factory), fchecks)
        report['factory'] = dict(checks=fchecks)

    if args.prev and args.card:
        print('== diff: whole card, %s vs %s ==' % (args.card, args.prev))
        only_a, only_b, diff, common_count = whole_card_diff(args.card, args.prev)
        print('only in %s: %r' % (args.card, only_a))
        print('only in %s: %r' % (args.prev, only_b))
        for path, sa, sb, ma, mb in diff:
            print('  DIFFERS %-26s size %s -> %s  md5 %s -> %s' % (path, sa, sb, ma, mb))

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
