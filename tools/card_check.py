#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a built card's road graph and coarse routing file against every format rule
measured off factory tiles, and diff a card against the previous one field by field.

    card_check.py --card A.img [--prev B.img] [--factory TILE ...] [--json]
    card_check.py --tiles TILE ... [--lev LEV3456R.DAT] [--factory TILE ...] [--json]

With --card, MAPAL001/*.DAT and LEV3456R.DAT are read straight off the FAT image (no
mount, no full-image load). With --tiles, the files are given directly - useful for a
quick check against a factory tile with no card to extract from.

Each rule below started as a one-off measurement script, checked against factory
tiles and a built card before being folded in here. A rule that fails on factory
data is a rule bug, not a card bug - run --factory and
read the factory column before trusting a FAIL on the card under test.

Exit status: 0 if every rule passes on the card under test, 1 otherwise (factory and
--prev results never affect the exit status).
"""
import argparse
import collections
import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg
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

def load_road_tiles(paths):
    """L1 {(row,col): parsed record}, L2 {(row,col): (parsed a, parsed part-b)},
    SH1 {(row,col): section-1 0x00fa Rec} over a set of tile files or bytes."""
    L1, L2, SH1 = {}, {}, {}
    for p in paths:
        with TileScan(p) as t:
            for r in t.records(section=2, level=1):
                try:
                    a = clarion_rd.parse_record(rd_check.part_a(r.plain(), 1))
                except Exception:
                    continue
                if 'nnodes' in a:
                    L1[clarion_bg.parcel_rowcol(1, *a['code'])] = a
            for r in t.records(section=2, level=2):
                pl = r.plain()
                try:
                    pa_b = rd_check.part_a(pl, 2)
                    a = clarion_rd.parse_record(pa_b)
                    b = clarion_rd.parse_part_b(pl[len(pa_b):])
                except Exception:
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
        for d in layer['dirs']:
            for row, rr in enumerate(d['rows']):
                for col, rec in enumerate(rr or []):
                    if rec:
                        a, b = rec['raw']
                        recs[row, col] = (clarion_rd.parse_record(a), clarion_rd.parse_part_b(b))
        out[layer['level']] = recs
    return out


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
    (checkw4.py). Factory is 100%."""
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
    (edgedeg.py L1/L2 mirroring + verify065.py L4/L5 w2/w4; census.py for the time field)."""
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
    """L2's (a) parts keyed the same way edgedeg.py/mirror_l1l2 want: {pc: parsed record}."""
    return {pc: a for pc, (a, b) in L2.items()}


def check_no_junction_on_parcel_line(L1, L2):
    """No junction (a node with more than one real link) sits on a parcel boundary - it
    would need an edge entry there and the factory never gives a border node more than
    one real link (edgedeg.py; the generic form of the card-068 'junctions quantized onto
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
    (vanish/w4chain.py, w4corr.py - correlation measured, not a factory-coded rule, so
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
    (LEV L4/L5 vs straight-line distance, l45unit.py's check, needs each record's own
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
    than one L2 parcel (components.py/verify065.py: a graph should exist wherever there
    is more than one L2 window of road data to connect)."""
    l2_windows = len(L2nodes(L2))
    st = collections.Counter(l2_windows=l2_windows, l4_records=len(lev4), l5_records=len(lev5))
    needs_graph = l2_windows > 1
    ok = (not needs_graph) or (len(lev4) > 0 and len(lev5) > 0)
    return result('coarse_graph_present', ok, st)


def run_all(L1, L2, SH1, lev4, lev5):
    out = []
    out.append(check_expansion(L1, L2))
    out.append(check_l2_triple_w4(L1, L2))
    out.append(check_edges(L1, L2, lev4, lev5))
    out.append(check_no_junction_on_parcel_line(L1, L2))
    out.append(check_node_order(lev4, lev5))
    out.append(check_l1_link_direction(L1, L2))
    out.append(check_l2_nflags(L2))
    out.append(check_roundabouts(L1, L2, SH1))
    out.append(check_length_time_units(L1, L2))
    out.append(check_coarse_graph_present(L2, lev4, lev5))
    return out


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
    """(only_in_a, only_in_b, [(path, size_a, size_b, md5_a, md5_b)] differing), like imgcmp."""
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
    return sorted(ka - kb), sorted(kb - ka), diff


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
    return L1, L2, SH1, lev.get(4, {}), lev.get(5, {})


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
    ap.add_argument('--tmp', default=None, help='scratch dir for extracted card files')
    args = ap.parse_args(argv)

    report = {}
    tmp_root = args.tmp or os.path.join(tempfile.gettempdir(), 'cardcheck')
    os.makedirs(tmp_root, exist_ok=True)

    if args.card:
        files = extract_card(args.card)
        card_tmp = os.path.join(tmp_root, os.path.splitext(os.path.basename(args.card))[0])
        os.makedirs(card_tmp, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(card_tmp, os.path.basename(name)), 'wb') as f:
                f.write(data)
        tiles = {k: v for k, v in files.items() if k.upper().startswith('MAPAL001/') and k.upper() != 'MAPAL001/LEV3456R.DAT'}
        lev = files.get('MAPAL001/LEV3456R.DAT') or next((v for k, v in files.items() if k.upper().endswith('LEV3456R.DAT')), None)
        L1, L2, SH1 = load_road_tiles(list(tiles.values()))
        levd = load_lev(lev)
        checks = run_all(L1, L2, SH1, levd.get(4, {}), levd.get(5, {}))
    else:
        L1, L2, SH1, lev4, lev5 = load_for_check(args.tiles, args.lev)
        checks = run_all(L1, L2, SH1, lev4, lev5)

    print_report(os.path.basename(args.card or 'tiles'), checks)
    report['card'] = dict(title=args.card or 'tiles', checks=checks)

    if args.factory:
        L1f, L2f, SH1f = load_road_tiles(args.factory)
        levf = load_lev(args.factory_lev)
        fchecks = run_all(L1f, L2f, SH1f, levf.get(4, {}), levf.get(5, {}))
        print_report('factory (%s)' % ', '.join(os.path.basename(f) for f in args.factory), fchecks)
        report['factory'] = dict(checks=fchecks)

    if args.prev and args.card:
        print('== diff: whole card, %s vs %s ==' % (args.card, args.prev))
        only_a, only_b, diff = whole_card_diff(args.card, args.prev)
        print('only in %s: %r' % (args.card, only_a))
        print('only in %s: %r' % (args.prev, only_b))
        for path, sa, sb, ma, mb in diff:
            print('  DIFFERS %-26s size %s -> %s  md5 %s -> %s' % (path, sa, sb, ma, mb))

        prev_files = extract_card(args.prev)
        print('identical: %d files' % (len(set(files) & set(prev_files)) - len(diff)))
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
        report['diff'] = dict(only_in_card=only_a, only_in_prev=only_b,
                              files_differing=[d[0] for d in diff],
                              fields=[dict(field=l, changed=n, xor=xor) for l, n, xor in field_rows])

    if args.json:
        print(json.dumps(report, indent=2, default=str))

    return 0 if all(c['passed'] for c in checks) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
