#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Section-2 level-1 records for every parcel of a tile cell, from OSM ways.

    ./rd_build.py --profile iceland TILE_IN TILE_OUT CELL [roads.json]
    --skip service,living_street   OSM classes left out altogether
    --simplify METRES              Douglas-Peucker tolerance
    --l2-classes wide|narrow|a,b,c which roads reach level 2
    --no-l2-graph                  leave out the section-2 level-2 directories
    --no-l2-names                  level-2 shapes without names
    --no-chain                     a graph node at every point, two points per shape

roads.json: [{id, hw, name, ref, nodes: [[lon, lat], ...]}, ...]. Ways are cut at parcel
edges; the cut points become edge nodes in both parcels, connected through a pseudo-entry
whose "other node" is the point's index in the neighbour's x-sorted node list. The drawn
shape's layer and class come from the OSM highway value through `fa_encode.CLASSES`, which is
what the renderer takes its road width from. One graph class group (0x0898) for every road.
Directories are added per mesh, the level table and the box table updated.
"""
import collections, json, math, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile, clarion_bg, clarion_profile, rd_encode, fa_encode
from clarion_bg import tile_cell, mesh_to_lon, mesh_to_lat, parcel_geo

PSIZE = {1: (1.875 / 60, 1.25 / 60), 2: (7.5 / 60, 5 / 60)}   # parcel size in degrees per level
DIRM = {1: 1, 2: 4}                                             # meshes per directory
SIDES = {'top': 0x8000, 'right': 0xa000, 'bottom': 0xc000, 'left': 0xe000}

# South-west mesh of the database grid. Only the remainder modulo the directory size matters,
# so this is what keeps level-2 directories on the grid the card already uses.
ORIGIN = (32, 904)


def use_profile(P):
    global ORIGIN
    ORIGIN = P.mesh_origin


def parcel_of(lon, lat, level=1):
    PW, PH = PSIZE[level]; m = DIRM[level]
    oy, ox = ORIGIN
    x = 924 + math.floor(lon); y = math.floor(lat * 1.5)
    y -= (y - oy) % m; x -= (x - ox) % m       # snap to the directory grid
    col = int((lon - mesh_to_lon(x)) / PW); row = int((lat - mesh_to_lat(y)) / PH)
    return (y, x, min(row, 31), min(col, 31))

def cut(pts, level=1):
    """Insert points where a polyline crosses a parcel edge; yield (parcel, [pts]) runs."""
    PW, PH = PSIZE[level]
    out = []; cur = None; run = []
    for a, b in zip(pts, pts[1:]):
        seg = [a]
        # split the segment at every parcel boundary it crosses (longitude and latitude lines)
        crossings = []
        for k in range(1, 200):
            lon = math.floor(min(a[0], b[0]) / PW) * PW + k * PW
            if lon >= max(a[0], b[0]): break
            crossings.append((lon - a[0]) / (b[0] - a[0]) if b[0] != a[0] else None)
        for k in range(1, 200):
            lat = math.floor(min(a[1], b[1]) / PH) * PH + k * PH
            if lat >= max(a[1], b[1]): break
            crossings.append((lat - a[1]) / (b[1] - a[1]) if b[1] != a[1] else None)
        for t in sorted(c for c in crossings if c is not None and 0 < c < 1):
            seg.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        seg.append(b)
        for p, q in zip(seg, seg[1:]):
            mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2); pc = parcel_of(*mid, level)
            if pc != cur:
                if run: out.append((cur, run))
                cur, run = pc, [p]
            run.append(q)
    if run: out.append((cur, run))
    return out

def quant(p, pc, level=1):
    lon0, lat0, w, h = parcel_geo(level, *pc)
    return (max(0, min(2047, round((p[0] - lon0) / w * 2048))), max(0, min(2047, round((p[1] - lat0) / h * 2048))))

def dist(a, b):
    m = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * 111320 * math.cos(m), (b[1] - a[1]) * 111320)

MAX_INNER = 40    # interior points a link may carry; a longer run gets a graph node here
MAX_POINTS = 64   # points per shape; a chain breaks at a link boundary before exceeding it

def build_graphs(ways, cell, level=1, chain=True):
    """Per parcel: nodes {key: ...} and links [(from key, to key, length, way id, name, hw,
    ref, inner points, chain id)]. With chain, a point is a graph node only where ways meet
    or end and the bends between nodes ride inside the link as its inner points, which is
    how the factory writes them. Without, every point is a node and every link two points,
    so the matcher sees a junction at every bend."""
    yb, xb = tile_cell(cell)
    parcels = {}
    for wy in ways:
        for pc, run in cut(wy['nodes'], level):
            y, x, row, col = pc
            if not (yb <= y < yb + 8 and xb <= x < xb + 8): continue
            P = parcels.setdefault(pc, dict(nodes={}, links=[], runs=[], touch=collections.Counter()))
            q = []
            for p in run:
                key = quant(p, pc, level)
                if not q or q[-1][0] != key: q.append((key, p))
            if len(q) < 2: continue
            P['runs'].append((q, wy))
            P['touch'][q[0][0]] += 2; P['touch'][q[-1][0]] += 2   # a run end is always a node
            for key, _ in q[1:-1]: P['touch'][key] += 1
    for pc, P in parcels.items():
        touch = P['touch']
        for rid, (q, wy) in enumerate(P['runs']):
            start = 0
            for i in range(1, len(q)):
                if chain and touch[q[i][0]] < 2 and i - start <= MAX_INNER: continue
                seg = q[start:i + 1]; start = i
                # a run that closes on itself is split in two, so no link joins a node to itself
                parts = [seg[:len(seg) // 2 + 1], seg[len(seg) // 2:]] if seg[0][0] == seg[-1][0] and len(seg) > 2 else [seg]
                for part in parts:
                    if part[0][0] == part[-1][0]: continue
                    for key, p in (part[0], part[-1]): P['nodes'].setdefault(key, dict(geo=p, edge=None))
                    length = sum(dist(part[j][1], part[j + 1][1]) for j in range(len(part) - 1))
                    P['links'].append((part[0][0], part[-1][0], max(1.0, length), wy['id'], wy.get('name'),
                                       wy['hw'], wy.get('ref'), [k for k, _ in part[1:-1]], rid))
        del P['runs'], P['touch']
        if chain: P['links'] = chain_links(P['links'])
    # edge nodes: coordinate on a parcel border; find the twin in the neighbour
    for pc, P in parcels.items():
        y, x, row, col = pc
        for key, nd in P['nodes'].items():
            gx, gy = key
            side = 'right' if gx == 2047 else 'left' if gx == 0 else 'top' if gy == 2047 else 'bottom' if gy == 0 else None
            if side: nd['edge'] = side
    for pc, P in parcels.items():
        keys = sorted(P['nodes'], key=lambda k: (k[0], k[1]))
        P['order'] = keys; P['index'] = {k: i for i, k in enumerate(keys)}
    return parcels

def chain_links(links):
    """Reorder links so the ones drawn as one shape are consecutive, and renumber their chain
    ids. A way's run through the parcel is one chain, junctions included, as the factory's
    are. Where a chain ends at a node that exactly one other chain of the same class, name
    and ref also ends at, the two are joined, so an OSM way split at an attribute change
    draws on as one road. A chain never closes on itself."""
    chains = {}
    for l in links: chains.setdefault(l[8], []).append(l)
    def key(c): return (c[0][5], c[0][4], c[0][6])
    def flip(c): return [(l[1], l[0]) + l[2:7] + (l[7][::-1], l[8]) for l in reversed(c)]
    ends = collections.defaultdict(list)                     # node -> [(chain id, 'a' start | 'b' end)]
    for cid, c in chains.items(): ends[c[0][0]].append((cid, 'a')); ends[c[-1][1]].append((cid, 'b'))
    for node in sorted(ends):
        bykey = collections.defaultdict(list)
        for c, s in ends[node]: bykey[key(chains[c])].append((c, s))
        pairs = [p for p in bykey.values() if len(p) == 2 and p[0][0] != p[1][0]]
        if len(pairs) != 1: continue                         # one road, two ends, nothing ambiguous
        (ca, sa), (cb, sb) = pairs[0]
        A, B = chains[ca], chains[cb]
        if sa == 'a': A = flip(A)                            # A must end at the node
        if sb == 'b': B = flip(B)                            # B must start there
        if A[0][0] == B[-1][1]: continue                     # joining would close the loop
        chains[ca] = A + B; del chains[cb]
        ends[A[0][0]] = [(ca, 'a') if c in (ca, cb) else (c, s) for c, s in ends[A[0][0]]]
        ends[B[-1][1]] = [(ca, 'b') if c in (ca, cb) else (c, s) for c, s in ends[B[-1][1]]]
    out = []; cid = 0
    for c in chains.values():
        # a closed way (roundabout, turning circle) is two shapes, as the factory draws them
        for part in ([c[:len(c) // 2], c[len(c) // 2:]] if c[0][0] == c[-1][1] and len(c) > 1 else [c]):
            out += [l[:8] + (cid,) for l in part]; cid += 1
    return out

def neighbour(pc, side):
    y, x, row, col = pc
    dr, dc = {'top': (1, 0), 'bottom': (-1, 0), 'right': (0, 1), 'left': (0, -1)}[side]
    row += dr; col += dc
    if row < 0: row, y = 31, y - 1
    if row > 31: row, y = 0, y + 1
    if col < 0: col, x = 31, x - 1
    if col > 31: col, x = 0, x + 1
    return (y, x, row, col)

def label(name, ref):
    """The shipped label text: `1=NAME;2=REF`, either part alone when the other is missing."""
    parts = ([f'1={name}'] if name else []) + ([f'2={ref}'] if ref else [])
    return ';'.join(parts)

def chains_of(links, idx, chain=True):
    """Shapes as [(link id, from index, to index, inner points)] chains: consecutive links of
    one way run, broken where the next link would take the shape past MAX_POINTS."""
    chains = []; npts = 0
    for lid, l in enumerate(links, start=1):
        item = (lid, idx[l[0]], idx[l[1]], l[7]); n = len(l[7]) + 1
        if chain and chains and links[lid - 2][8] == l[8] and npts + n <= MAX_POINTS:
            chains[-1].append(item); npts += n
        else:
            chains.append([item]); npts = n + 1
    return chains

def encode_parcels(parcels, level=1, no_names=False, chain=True):
    out = {}
    for pc, P in parcels.items():
        keys = P['order']; idx = P['index']
        nodes = list(keys)
        links = [(idx[a], idx[b], ln) for a, b, ln, *_ in P['links']]
        edges = {}
        for key, nd in P['nodes'].items():
            if not nd['edge']: continue
            npc = neighbour(pc, nd['edge']); twin = None
            if npc in parcels:
                gx, gy = key
                tw = (0 if nd['edge'] == 'right' else 2047 if nd['edge'] == 'left' else gx, 0 if nd['edge'] == 'top' else 2047 if nd['edge'] == 'bottom' else gy)
                twin = parcels[npc]['index'].get(tw)
                if twin is None:   # nearest node on that edge of the neighbour
                    cands = [(abs(k[0] - tw[0]) + abs(k[1] - tw[1]), i) for k, i in parcels[npc]['index'].items() if (k[0] == tw[0] if nd['edge'] in ('left', 'right') else k[1] == tw[1])]
                    twin = min(cands)[1] if cands else None
            if twin is not None: edges[idx[key]] = (SIDES[nd['edge']], twin)
        out[pc] = rd_encode.build_record_edges(level, pc[0], pc[1], pc[2], pc[3], nodes, links, edges, classes=[l[5] for l in P['links']])
        # drawn geometry: one shape per chain of links, each pointing at its street's label entry
        chains = chains_of(P['links'], idx, chain)
        hws = [P['links'][c[0][0] - 1][5] for c in chains]
        label_of = {c[0][0]: label(P['links'][c[0][0] - 1][4], P['links'][c[0][0] - 1][6]) for c in chains}
        labels = []
        for c in chains:
            n = label_of.get(c[0][0])
            if n and n not in labels: labels.append(n)
        # offsets first (the section layout only depends on the labels and the item count)
        _, offs = fa_encode.names_section(labels, [[0] * 9] * len(chains))
        name_offs = [0xffff] * len(chains) if no_names else [offs.get(label_of.get(c[0][0]), 0xffff) for c in chains]
        fa = fa_encode.build_record(level, pc[0], pc[1], pc[2], pc[3], nodes, chains, name_offs=name_offs, hws=hws)
        section, _ = fa_encode.names_section(labels, fa_encode.build_record.last_items)
        out[pc] = out[pc] + fa + (None if no_names else section,)
    return out

def simplify(pts, tol):
    """Douglas-Peucker in metres, endpoints kept."""
    if len(pts) < 3: return pts
    lat = math.radians(pts[0][1]); kx, ky = 111320 * math.cos(lat), 111320
    def d2(p, a, b):
        ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky; bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
        L = bx * bx + by * by
        t = 0 if L == 0 else max(0, min(1, -(ax * bx + ay * by) / L))
        return (ax + bx * t) ** 2 + (ay + by * t) ** 2
    keep = [0, len(pts) - 1]; stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2: continue
        k, dm = max(((k, d2(pts[k], pts[i], pts[j])) for k in range(i + 1, j)), key=lambda t: t[1])
        if dm > tol * tol: keep.append(k); stack += [(i, k), (k, j)]
    return [pts[k] for k in sorted(keep)]

def labels_unpadded(labels):
    """Label bytes without the section's trailing pad (entries are self-delimiting)."""
    p = 0; n = 0
    while p + 10 <= len(labels):
        typ, flags = struct.unpack_from('>HH', labels, p)
        if typ == 0 and flags == 0: break
        q = p + 8 + (2 if flags & 0x4000 else 0); ln = struct.unpack_from('>H', labels, q)[0]; p = q + 2 + ln
    return labels[:p]

def count_labels(b):
    p = 0; n = 0
    while p + 10 <= len(b):
        typ, flags = struct.unpack_from('>HH', b, p); q = p + 8 + (2 if flags & 0x4000 else 0)
        ln = struct.unpack_from('>H', b, q)[0]; p = q + 2 + ln; n += 1
    return n

def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    skip, tol, l2 = set(P.skip), P.simplify_metres, P.l2_classes
    no_l2_graph, no_l2_names = not P.l2_graph, False
    if '--skip' in argv:
        i = argv.index('--skip'); skip = set(argv[i + 1].split(',')); del argv[i:i + 2]
    if '--simplify' in argv:
        i = argv.index('--simplify'); tol = float(argv[i + 1]); del argv[i:i + 2]
    if '--no-l2-graph' in argv: argv.remove('--no-l2-graph'); no_l2_graph = True
    if '--l2-graph' in argv: argv.remove('--l2-graph'); no_l2_graph = False
    if '--no-l2-names' in argv: argv.remove('--no-l2-names'); no_l2_names = True
    no_chain = '--no-chain' in argv
    if no_chain: argv.remove('--no-chain')
    if '--l2-classes' in argv:
        i = argv.index('--l2-classes'); l2 = clarion_profile.l2_classes(argv[i + 1]); del argv[i:i + 2]
    src, dest, cell = argv[:3]
    rpath = argv[3] if len(argv) > 3 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    ways = [w for w in json.load(open(rpath)) if w['hw'] not in skip]
    if tol:
        for w in ways: w['nodes'] = simplify(w['nodes'], tol)
    d = open(src, 'rb').read(); t = clarion_tile.read_tile(d)
    sec1 = [s for s in t['sections'] if s['id'] == 1][0]; sec2 = [s for s in t['sections'] if s['id'] == 2][0]
    boxes = {}; report = []
    for level, subset in ((1, ways), (2, [w for w in ways if w['hw'] in l2])):
        # level 2 keeps one two-point shape per link: the matcher reads level 1 only, and the
        # factory's chained level-2 form has a header this file has not decoded
        chain = level == 1 and not no_chain
        parcels = build_graphs(subset, cell, level, chain)
        recs = encode_parcels(parcels, level, no_names=(level == 2 and no_l2_names), chain=chain)
        shapes = [c for P in parcels.values() for c in chains_of(P['links'], P['index'], chain)]
        lv = [L for L in sec2['levels'] if L['level'] == level][0]; lv['dirs'] = []
        per_dir = {}
        for (y, x, row, col), (plain, comp, fplain, fcomp, _) in recs.items():
            per_dir.setdefault((y, x), {}).setdefault(row, {})[col] = (plain, comp)
        for (y, x), rows in sorted(per_dir.items()):
            if not (level == 2 and no_l2_graph): lv['dirs'].append(rd_encode.make_dir(level, y, x, rows))
        bydir = {(D['y'], D['x']): D for L in sec1['levels'] if L['level'] == level for D in L['dirs']}
        placed = place_shapes(recs, bydir)
        PW, PH = PSIZE[level]
        if recs and lv['dirs']:   # the box table only advertises levels that have road directories
            lats = [parcel_geo(level, *pc)[1] for pc in recs] + [parcel_geo(level, *pc)[1] + PH for pc in recs]
            lons = [parcel_geo(level, *pc)[0] for pc in recs] + [parcel_geo(level, *pc)[0] + PW for pc in recs]
            boxes[level] = (math.ceil(max(lats) * 60), math.floor(min(lats) * 60),
                            math.floor(min(lons) * 60), math.ceil(max(lons) * 60))
        counts = sorted(len(Q['nodes']) for Q in parcels.values()) or [0]
        report.append(f'level {level}: {len(recs)} parcels in {len(lv["dirs"])} dirs, '
                      f'{sum(len(Q["nodes"]) for Q in parcels.values())} nodes, shapes placed {placed}, '
                      f'nodes/parcel median {counts[len(counts)//2]} max {counts[-1]}')
        if shapes:   # how far the chaining got: two points and one link per shape is none of it
            pts = sorted(1 + sum(len(inner) + 1 for _, _, _, inner in c) for c in shapes)
            multi = sum(1 for c in shapes if len(c) > 1)
            report.append(f'  shapes {len(shapes)}: points/shape median {pts[len(pts)//2]} '
                          f'mean {sum(pts)/len(pts):.2f} max {pts[-1]}, '
                          f'multi-link {100 * multi / len(shapes):.1f}%')
    nw, _, _ = clarion_tile.table_words(sec2); sec2['count'] = -(-2 * nw // 256)
    hdr = bytearray(t['header'])
    p = 0x1ac + 2; groups = hdr[p - 2] << 8 | hdr[p - 1]
    for _ in range(groups):
        sid, cnt = hdr[p], hdr[p + 1]; p += 2
        for _ in range(cnt):
            if sid == 2 and hdr[p] in (1, 2):
                struct.pack_into('>H', hdr, p + 2, len([L for L in sec2['levels'] if L['level'] == hdr[p]][0]['dirs']))
            p += 4
    bt = hdr[0x2ac:0x2ac + 0x30]; i = bt.find(b'\x02\x00\xff\xff'); assert i > 0
    new = bt[:i] + bytes([2, len(boxes)])
    for level in sorted(boxes, reverse=True): new += struct.pack('>BB4h', level, 0, *boxes[level])
    new += b'\xff' * (0x30 - len(new)); hdr[0x2ac:0x2ac + 0x30] = new
    t['header'] = bytes(hdr)
    out = clarion_tile.write_tile(t, clarion_tile.default_order(t)); open(dest, 'wb').write(out)
    print(f'wrote {dest} {len(out)} bytes; ways used {len(ways)}')
    for r in report: print('  ' + r)


def place_shapes(recs, bydir):
    """Road-shape records into the even pairs of the background directories, names into the
    background records (road names first, then whatever was there)."""
    placed = 0
    for (y, x, row, col), (plain, comp, fplain, fcomp, section) in recs.items():
        D = bydir.get((y, x))
        if D is None or row not in D['slots']: continue
        s = D['slots'][row]
        if not any(r['k'] == 2 * col + 1 for r in s['recs']): continue
        s['recs'] = [r for r in s['recs'] if r['k'] != 2 * col]
        bi = [i for i, r in enumerate(s['recs']) if r['k'] == 2 * col + 1][0]
        if section is None:   # shapes only, background record untouched
            s['recs'].insert(bi, dict(k=2 * col, raw=fcomp, words=len(fplain) // 2))
            body2 = bytearray(s['body']); struct.pack_into('>II', body2, 8 * (2 * col), len(fcomp) // 2, len(fplain) // 2); s['body'] = bytes(body2); placed += 1
            continue
        bgrec = s['recs'][bi]; r = clarion_bg.parse_record(zlib.decompress(bgrec['raw']))
        old = r['sections'].get('names'); keep = labels_unpadded(old[24:4 * struct.unpack_from('>H', old, 8)[0]] if struct.unpack_from('>H', old, 8)[0] != 0xffff else old[24:]) if old else b''
        hdr = bytearray(section[:24]); nl = struct.unpack_from('>H', section, 2)[0]
        sub_off = struct.unpack_from('>H', section, 8)[0]; labels = section[24:4 * sub_off]; sub = section[4 * sub_off:]
        body = labels_unpadded(labels) + keep
        while len(body) % 4: body += b'\0'
        struct.pack_into('>H', hdr, 2, nl + count_labels(keep)); struct.pack_into('>H', hdr, 8, (24 + len(body)) // 4)
        r['sections']['names'] = bytes(hdr) + body + sub
        newpl = clarion_bg.build_record(r); c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); newc = c.compress(newpl) + c.flush()
        if len(newc) % 2: newc += b'\0'
        bgrec['raw'] = newc; bgrec['words'] = len(newpl) // 2
        s['recs'].insert(bi, dict(k=2 * col, raw=fcomp, words=len(fplain) // 2))
        body2 = bytearray(s['body']); struct.pack_into('>II', body2, 8 * (2 * col), len(fcomp) // 2, len(fplain) // 2)
        struct.pack_into('>II', body2, 8 * (2 * col + 1), len(newc) // 2, len(newpl) // 2); s['body'] = bytes(body2); placed += 1
    return placed

if __name__ == '__main__':
    main()
