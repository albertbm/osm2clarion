#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the level-4 and level-5 routing graphs in LEV3456R.DAT from the level-2 road graph."""
import collections
import os
import struct

import clarion_bg
import clarion_rd
import lev_file
import rd_build
import rd_encode
from tile_scan import TileScan

FACTORY_CLASSES = {4: set(range(16)), 5: set(range(7)) | {10, 11, 12}}


def part_a(raw):
    units = [struct.unpack_from('>II', raw, 12 + 8 * i) for i in range(5)]
    return raw[:2 * max(o + n for o, n in units if o != 0xffffffff)]


def unscaled(word):
    return (word & 0xfff) * 4 ** ((word >> 12) & 7)


def emitted_l2_layer(paths):
    """Read the exact L2 nodes, indexes and link metadata emitted in the road tiles."""
    records = {}
    for path in paths:
        with TileScan(path) as tile:
            for rr in tile.records(section=2, level=2):
                rec = clarion_rd.parse_record(part_a(rr.plain()))
                pc = clarion_bg.parcel_rowcol(2, *rec['code'])
                assert pc not in records, 'duplicate L2 parcel %r in %s' % (pc, path)
                records[pc] = rec
    nodes = collections.defaultdict(list); links = []
    for pc, rec in sorted(records.items()):
        gids = {}
        for i, nd in enumerate(rec['nodes']):
            sides = {e['cls'] & 0xe000 for e in nd['entries'] if e['cls'] & 0x8000}
            if sides:
                # both copies of a parcel-line node share one id: the copy at 2047 is the
                # neighbour's copy at 0, one unit further on
                x = 2048 if 0xa000 in sides and nd['x'] == 2047 else nd['x']
                y = 2048 if 0x8000 in sides and nd['y'] == 2047 else nd['y']
                gid = ('edge', pc[1] * 2048 + x, pc[0] * 2048 + y)
            else:
                gid = (pc, i)
            gids[i] = gid; nodes[gid].append((pc, i))
        ends = collections.defaultdict(list)
        for i, nd in enumerate(rec['nodes']):
            for e in nd['entries']:
                if not e['cls'] & 0x8000:
                    ends[e['cls'] & 0xf, e['id'] & 0x1fff].append((i, e))
        for (cls, lid), pair in sorted(ends.items()):
            assert len(pair) == 2, (pc, cls, lid, len(pair))
            (i, ei), (j, ej) = pair
            length = (unscaled(ei['b']) + unscaled(ej['b'])) / 2
            tenths = (unscaled(ei['a']) + unscaled(ej['a'])) / 2
            speed = length / (tenths / 10) * 3.6
            bits = ei['w5'] & 0xc000, ej['w5'] & 0xc000
            direction = 1 if bits == (0x4000, 0x8000) else -1 if bits == (0x8000, 0x4000) else 0
            assert ei['w4'] == ej['w4'], (pc, cls, lid, ei['w4'], ej['w4'])
            links.append(dict(a=gids[i], b=gids[j], length=length, cls=cls, speed=speed,
                              direction=direction, w4=ei['w4'], parcel=pc, child=(pc, lid),
                              next_a=gids[j], next_b=gids[i], pa=pc, pb=pc))
    return dict(nodes=nodes, links=links, level=2, records=records, paths=list(paths))


def parent_of(level, parcel):
    """The level-4 or level-5 parcel containing one child parcel."""
    if level == 4:
        row, col = parcel
        return (row - rd_build.ORIGIN[0] * 8) // 16, (col - rd_build.ORIGIN[1] * 8) // 16
    return parcel[0] // 4, parcel[1] // 4


def child_word(level, parcel):
    """The child parcel's address inside its parent, measured from factory part B."""
    if level == 4:
        row, col = parcel
        r = (row - rd_build.ORIGIN[0] * 8) % 16
        c = (col - rd_build.ORIGIN[1] * 8) % 16
        return r << 12 | c << 4
    return parcel[0] % 4 << 14 | parcel[1] % 4 << 6


def child_node_ref(child, gid, parent, level):
    choices = [(pc, i) for pc, i in child['nodes'][gid] if parent_of(level, pc) == parent]
    assert choices, (level, parent, gid, child['nodes'][gid])
    return sorted(choices)[0]


def quant(level, parent, geo, origin):
    lon0, lat0, w, h = clarion_bg.parcel_geo(level, origin[0], origin[1], *parent)
    return (max(0, min(2047, round((geo[0] - lon0) / w * 2048))),
            max(0, min(2047, round((geo[1] - lat0) / h * 2048))))


def geo_of(child, gid):
    pc, i = child['nodes'][gid][0]
    if child['level'] == 2:
        nd = child['records'][pc]['nodes'][i]
        return pc[1] / 8 - 924 + nd['x'] / 16384, pc[0] / 12 + nd['y'] / 24576
    return child['geo'][gid]


def marker(level, row, col):
    """The five rotating child-grid selectors used by factory level-4 and level-5 part B."""
    k = (row - col) % 5 if level == 4 else (col - row) % 5
    return 0x2100 + 0x2000 * k


def collapse(per):
    """Collapse compatible degree-two child nodes without crossing a target-parcel boundary."""
    owners = collections.defaultdict(set)
    for parent, links in per.items():
        for link in links:
            owners[link['a']].add(parent); owners[link['b']].add(parent)
    for parent, links in per.items():
        changed = True
        while changed:
            changed = False; inc = collections.defaultdict(list)
            for k, link in enumerate(links):
                inc[link['a']].append(k); inc[link['b']].append(k)
            for node, at in inc.items():
                if len(at) != 2 or len(owners[node]) != 1: continue
                p, q = at; a, b = links[p], links[q]
                if a['cls'] != b['cls']: continue
                x = a['b'] if a['a'] == node else a['a']
                y = b['b'] if b['a'] == node else b['a']
                if x == y: continue
                def allows(link, u, v):
                    return (link['direction'] == 0 or
                            link['direction'] > 0 and (link['a'], link['b']) == (u, v) or
                            link['direction'] < 0 and (link['b'], link['a']) == (u, v))
                fwd = allows(a, x, node) and allows(b, node, y)
                back = allows(b, y, node) and allows(a, node, x)
                if not fwd and not back: continue
                length = a['length'] + b['length']
                speed = length / (a['length'] / a['speed'] + b['length'] / b['speed'])
                merged = dict(a=x, b=y, length=length, cls=a['cls'], speed=speed,
                              direction=0 if fwd and back else 1 if fwd else -1,
                              w4=a.get('w4', 0), parcel=a['parcel'], child=a['child'],
                              next_a=a['next_a' if a['a'] == x else 'next_b'],
                              pa=a['pa' if a['a'] == x else 'pb'],
                              next_b=b['next_b' if b['b'] == y else 'next_a'],
                              pb=b['pb' if b['b'] == y else 'pa'])
                links[:] = [link for k, link in enumerate(links) if k not in (p, q)] + [merged]
                changed = True; break


def pairing_record(level, origin, parent, country, code, ordered, refs, adjacency):
    """Factory-form part B: blocks sorted by child parcel/node, each mapped to an upper node."""
    blocks = []
    for upper in range(len(ordered)):
        parcel, child = refs[upper]
        blocks.append((parcel, upper, child, adjacency[upper]))
    unit = [len(blocks), marker(level, *parent)]
    for parcel, upper, child, entries in sorted(blocks):
        unit += [len(entries) << 12, upper, parcel, child]
        for e in entries: unit += e
    unit += [code[0], code[1]]
    head, _ = rd_encode.record_head(level, origin[0], origin[1], parent[0], parent[1],
                                    [(0xffffffff, 0), (0x1b, len(unit))], country)
    return head + struct.pack('>%dH' % len(unit), *unit)


SIDE = {(-1, 0): 0xe000, (1, 0): 0xa000, (0, -1): 0xc000, (0, 1): 0x8000}


def next_ref(child, level, own, gid, next_gid, first_pc):
    """A link triple's child reference: the next child node along the link in the node's own
    child parcel, or, when the link leaves through the node's other copy across a child-parcel
    line, that copy itself. Factory L4 and L5 link triples all fit this (coarse064)."""
    own_pc, _ = own
    if first_pc == own_pc:
        copies = [k for pc, k in child['nodes'][next_gid] if pc == own_pc]
    else:
        copies = [k for pc, k in child['nodes'][gid] if pc == first_pc]
    assert copies, (level, own, gid, next_gid, first_pc)
    return child_word(level, first_pc), min(copies)


def encode_level(child, records, level, origin, country, occurrences, hierarchy=None):
    """Encode prepared records after the next level's hierarchy membership is known."""
    hierarchy = hierarchy or set()
    for parent, R in records.items():
        nodes = R['nodes']
        links = [(R['pos'][l['a']], R['pos'][l['b']], l['length']) for l in R['links']]
        classes = [l['cls'] for l in R['links']]; speeds = [l['speed'] for l in R['links']]
        dirs = [l['direction'] for l in R['links']]; w4 = [l.get('w4', 0) for l in R['links']]
        counts = collections.Counter(); ids = []
        for cls in classes:
            counts[cls] += 1; ids.append(counts[cls])
        edges = {}; edge_far = {}
        for i, gid in enumerate(R['gids']):
            for far_parent, far_i in occurrences[gid]:
                delta = far_parent[1] - parent[1], far_parent[0] - parent[0]
                if delta in SIDE:
                    edges[i] = (SIDE[delta], far_i); edge_far[i] = (far_parent, far_i); break
        flags = [0x2000 if gid in hierarchy else 0 for gid in R['gids']]
        a, _ = rd_encode.build_record_edges(level, origin[0], origin[1], parent[0], parent[1],
                                            nodes, links, edges, compress=False, class_codes=classes,
                                            speeds=speeds, country=country, dirs=dirs, ids=ids,
                                            node_flags=flags, link_w4=w4)
        refs = R['refs']
        adjacency = [[] for _ in nodes]
        for l in R['links']:
            i, j = R['pos'][l['a']], R['pos'][l['b']]
            adjacency[i].append((j,) + next_ref(child, level, R['refpc'][i], l['a'], l['next_a'], l['pa']))
            adjacency[j].append((i,) + next_ref(child, level, R['refpc'][j], l['b'], l['next_b'], l['pb']))
        for i, (far_parent, far_i) in edge_far.items():
            far_ref = records[far_parent]['refs'][far_i]
            adjacency[i].append((far_i, far_ref[0], far_ref[1]))
        code = struct.unpack_from('>HH', a)
        b = pairing_record(level, origin, parent, country, code, R['gids'], refs, adjacency)
        pa = clarion_rd.parse_record(a); pb = clarion_rd.parse_part_b(b)
        assert clarion_rd.encode_record(pa) == a and clarion_rd.encode_part_b(pb) == b
        assert sorted(n['upper'] for n in pb['nodes']) == list(range(len(pa['nodes'])))
        # part B addresses part-A nodes by index, so the encoder must have kept our node order
        assert [(n['x'], n['y']) for n in pa['nodes']] == nodes, (level, parent)
        R.update(a=a, b=b, edges=edges)


def build_level(child, level, origin, country, allowed=None):
    """Reparcel one child graph, retaining its topology and adding resolving down-pointers."""
    per = collections.defaultdict(list)
    for link in child['links']:
        if allowed is not None and link['cls'] not in allowed: continue
        per[parent_of(level, link['parcel'])].append(dict(link))
    collapse(per)
    occurrences = collections.defaultdict(list); records = {}
    for parent, links in sorted(per.items()):
        gids = []
        for link in links:
            for gid in (link['a'], link['b']):
                if gid not in gids: gids.append(gid)
        ncls = {}
        for link in links:
            for gid in (link['a'], link['b']): ncls[gid] = min(ncls.get(gid, 15), link['cls'])
        gids.sort(key=lambda gid: (ncls[gid], quant(level, parent, geo_of(child, gid), origin), repr(gid)))
        pos = {gid: i for i, gid in enumerate(gids)}
        for gid in gids: occurrences[gid].append((parent, pos[gid]))
        records[parent] = dict(gids=gids, pos=pos, links=links)
    for parent, R in records.items():
        R['nodes'] = [quant(level, parent, geo_of(child, gid), origin) for gid in R['gids']]
        R['refpc'] = [child_node_ref(child, gid, parent, level) for gid in R['gids']]
        R['refs'] = [(child_word(level, pc), i) for pc, i in R['refpc']]
    encode_level(child, records, level, origin, country, occurrences)
    geo = {gid: geo_of(child, gid) for gid in occurrences}
    nodes = collections.defaultdict(list)
    links = []
    for parent, R in records.items():
        for gid, i in R['pos'].items(): nodes[gid].append((parent, i))
        for link in R['links']:
            links.append(dict(link, parcel=parent, child=(parent, 0), next_a=link['b'], next_b=link['a'],
                              pa=parent, pb=parent))
    return records, dict(nodes=nodes, links=links, level=level, geo=geo), occurrences


def directory(level, records, origin, box):
    n = {4: 44, 5: 11}[level]
    rows = []
    for row in range(n):
        recs = [None] * n
        for col in range(n):
            if (row, col) in records:
                R = records[(row, col)]; recs[col] = lev_file.rawrec(R['a'], R['b'])
        rows.append(recs if any(recs) else None)
    return dict(box=box, cols=n, y=origin[0], x=(origin[1] - 1024) & 0xffff,
                typ=0x81, pairs=n, rows=rows)


def patch_l2_flags(paths, flags):
    """Set hierarchy flags in emitted L2 records without changing tile layout or record sizes."""
    found = set()
    for path in paths:
        data = bytearray(open(path, 'rb').read())
        with TileScan(bytes(data)) as tile:
            for rr in tile.records(section=2, level=2):
                rec = clarion_rd.parse_record(part_a(rr.plain()))
                pc = clarion_bg.parcel_rowcol(2, *rec['code'])
                u0 = rec['pairs'][0][0]
                for i, nd in enumerate(rec['nodes']):
                    flag = flags.get((pc, i), 0)
                    struct.pack_into('>H', data, rr.off + 2 * (u0 + nd['at'] + 2),
                                     nd['nflags'] & ~0x6000 | flag)
                    if flag: found.add((pc, i))
        tmp = path + '.hierarchy.tmp'
        with open(tmp, 'wb') as f: f.write(data)
        os.replace(tmp, path)
    assert found == set(flags), 'L2 hierarchy nodes absent from tiles: %r' % sorted(set(flags) - found)[:10]


def build(ways, profile, l2_tiles, bbox=None):
    """({4: directory, 5: directory}, statistics) for lev_build.

    bbox=(lon0, lat0, lon1, lat1) keeps only the emitted L2 links with both ends inside
    it, for a minimal test graph, and skips patching hierarchy flags into the L2 tiles
    (those flags are not needed for a down-pointer to resolve, only, if at all, for the
    firmware to find the hierarchy from the fine side). Off by default; with no bbox this
    is the normal full graph, unchanged.
    """
    rd_build.use_profile(profile)
    if not l2_tiles: raise ValueError('coarse routing requires the emitted L2 road tiles')
    child = emitted_l2_layer(l2_tiles)
    if bbox is not None:
        lon0, lat0, lon1, lat1 = bbox
        def inside(gid):
            lon, lat = geo_of(child, gid)
            return lon0 <= lon <= lon1 and lat0 <= lat <= lat1
        child['links'] = [l for l in child['links'] if inside(l['a']) and inside(l['b'])]
    origin = profile.mesh_origin
    r4, layer4, occ4 = build_level(child, 4, origin, profile.country, FACTORY_CLASSES[4])
    r5, layer5, _ = build_level(layer4, 5, origin, profile.country, FACTORY_CLASSES[5])
    upper5 = set(layer5['nodes'])
    encode_level(child, r4, 4, origin, profile.country, occ4, upper5)
    flags = {}
    for parent, R in r4.items():
        for gid in R['gids']:
            pc, i = child_node_ref(child, gid, parent, 4)
            flags[pc, i] = 0x4000 if gid in upper5 else 0x2000
    if bbox is None:
        patch_l2_flags(l2_tiles, flags)
    box = (origin[0] + 88, origin[0], origin[1], origin[1] + 88)
    dirs = {4: directory(4, r4, origin, box), 5: directory(5, r5, origin, box)}
    stats = {4: (len(r4), sum(len(r['nodes']) for r in r4.values()), sum(len(r['links']) for r in r4.values())),
             5: (len(r5), sum(len(r['nodes']) for r in r5.values()), sum(len(r['links']) for r in r5.values()))}
    stats['l2_patched'] = bbox is None
    stats['l2_flags'] = collections.Counter(flags.values())
    return dirs, stats


def check(records, level):
    """Round-trip both parts and verify every part-B node maps to its part-A node."""
    bad = []
    for parent, R in records.items():
        a = clarion_rd.parse_record(R['a']); b = clarion_rd.parse_part_b(R['b'])
        if clarion_rd.encode_record(a) != R['a'] or clarion_rd.encode_part_b(b) != R['b']:
            bad.append((level, parent, 'round-trip'))
        if sorted(n['upper'] for n in b['nodes']) != list(range(len(a['nodes']))):
            bad.append((level, parent, 'pairing'))
    return bad
