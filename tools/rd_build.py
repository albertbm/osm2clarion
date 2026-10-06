#!/usr/bin/env python3
"""Section-2 level-1 records for every parcel of a tile cell, from OSM ways.

    ./rd_build.py --profile iceland TILE_IN TILE_OUT CELL [roads.json]
    --skip service,living_street   OSM classes left out altogether
    --simplify METRES              Douglas-Peucker tolerance
    --l2-classes wide|narrow|a,b,c which roads reach level 2
    --no-l2-graph                  leave out the section-2 level-2 directories
    --no-l2-names                  level-2 shapes without names

roads.json: [{id, hw, name, ref, nodes: [[lon, lat], ...]}, ...]. Ways are cut at parcel
edges; the cut points become edge nodes in both parcels, connected through a pseudo-entry
whose "other node" is the point's index in the neighbour's x-sorted node list. The drawn
shape's layer and class come from the OSM highway value through `fa_encode.CLASSES`, which is
what the renderer takes its road width from. One graph class group (0x0898) for every road.
Directories are added per mesh, the level table and the box table updated.
"""
import json, math, os, struct, sys, zlib
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

def build_graphs(ways, cell, level=1):
    yb, xb = tile_cell(cell)
    parcels = {}
    for wy in ways:
        for pc, run in cut(wy['nodes'], level):
            y, x, row, col = pc
            if not (yb <= y < yb + 8 and xb <= x < xb + 8): continue
            P = parcels.setdefault(pc, dict(nodes={}, links=[]))
            def nid(p):
                key = quant(p, pc, level)
                if key not in P['nodes']: P['nodes'][key] = dict(geo=p, edge=None)
                return key
            for a, b in zip(run, run[1:]):
                ka, kb = nid(a), nid(b)
                if ka == kb: continue
                P['links'].append((ka, kb, max(1.0, dist(a, b)), wy['id'], wy.get('name'), wy['hw'], wy.get('ref')))
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

def encode_parcels(parcels, level=1, no_names=False):
    out = {}
    for pc, P in parcels.items():
        keys = P['order']; idx = P['index']
        nodes = list(keys)
        links = [(idx[a], idx[b], ln) for a, b, ln, _, _, _, _ in P['links']]
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
        # drawn geometry: one shape per link, each pointing at its street's label entry
        way_of = {lid: l[3] for lid, l in enumerate(P['links'], start=1)}
        hw_of = {lid: l[5] for lid, l in enumerate(P['links'], start=1)}
        label_of = {lid: label(l[4], l[6]) for lid, l in enumerate(P['links'], start=1)}
        chains = fa_encode.chains_from_links(links, way_of)
        hws = [hw_of.get(c[0][0]) for c in chains]
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
        parcels = build_graphs(subset, cell, level)
        recs = encode_parcels(parcels, level, no_names=(level == 2 and no_l2_names))
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
