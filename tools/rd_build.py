#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Section-2 level-1 records for every parcel of a tile cell, from OSM ways. Ways are cut at parcel edges into edge nodes linked through a pseudo-entry across parcels."""
import collections, json, math, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile, clarion_bg, clarion_format, clarion_profile, rd_encode, fa_encode, shape_index, osm_roads, sec4_write
from clarion_bg import tile_cell, mesh_to_lon, mesh_to_lat, parcel_geo

PSIZE = {1: (1.875 / 60, 1.25 / 60), 2: (7.5 / 60, 5 / 60)}   # degrees per parcel
DIRM = {1: 1, 2: 4}
SIDES = {'top': 0x8000, 'right': 0xa000, 'bottom': 0xc000, 'left': 0xe000}

# keeps level-2 directories on the card's existing grid (only the remainder mod directory size matters)
ORIGIN = (32, 904)
COUNTRY = None     # ISO 3166 numeric code for every section-2 record, from the profile
SPEED_UNIT = 'km/h'   # the unit the country's signs are posted in, from the profile
FMT = clarion_format.DEFAULT
# --keep-names: (level, y, x, row, col) -> (label bytes, count) of the place, water and POI names
# already in that parcel's background record
KEEP_BASE = None
# QY7: (level, y, x, row, col) -> the three area codes the source tile's own names items carry there
AREA_BASE = None


def use_profile(P):
    global ORIGIN, COUNTRY, SPEED_UNIT, FMT
    ORIGIN = P.mesh_origin
    COUNTRY = P.country
    SPEED_UNIT = P.speed_unit
    FMT = rd_encode.FMT = fa_encode.FMT = P.fmt


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

MAX_INNER = 40    # longer runs get a graph node instead of riding inside the link
MAX_POINTS = 64

def off_parcel_lines(ways):
    """Move a junction that quantizes onto a level-1 parcel line (level-2 lines are a subset) one unit
    into the parcel east or north of it. A factory parcel-edge node has exactly one real link (113,466 of
    113,466 L1 and 69,498 of 69,498 L2 nodes on B30R070R/B48R060R): a junction never sits on a line.
    Left there, the hierarchy pass takes the edge entry's twin index for a node of the own record
    (card 065/066, PC 0x364d44)."""
    shared = collections.Counter(tuple(p) for w in ways for p in w['nodes'])
    def off(v, per):
        # within half a unit of a line at this level's scale (32 x 48 parcels per degree at level 1,
        # 8 x 12 at level 2): 2046.5 and up rounds to the cut node's key 2047 (2048 is clamped)
        f = v * per * 2048; r = f - math.floor(f / 2048) * 2048
        if r < 0.5 or r > 2046.5: return (math.floor(f / 2048 + 0.5) * 2048 + 1.125) / (per * 2048)
        return None
    moved = {}
    for p, n in shared.items():
        if n < 2: continue
        lon, lat = p
        # each axis on its own: a level-2 line is a level-1 line too, so the level-2 step
        # (1.125 level-2 units = 4.5 level-1 units, about 3 m) clears both; else 4.5 level-1 units
        nlon = off(lon + 924, 8) or off(lon + 924, 32)
        nlat = off(lat, 12) or off(lat, 48)
        if nlon is not None or nlat is not None:
            moved[p] = [nlon - 924 if nlon is not None else lon, nlat if nlat is not None else lat]
    for w in ways: w['nodes'] = [moved.get(tuple(q), q) for q in w['nodes']]
    return moved

def build_graphs(ways, cell, level=1, chain=True):
    """Per parcel: nodes {key: ...} and links [(from, to, length, way id, name, hw, ref, maxspeed, inner points, chain id, one-way)]."""
    yb, xb = tile_cell(cell)
    def inside(pc): return yb <= pc[0] < yb + 8 and xb <= pc[1] < xb + 8
    parcels = {}
    for wy in ways:
        for pc, run in cut(wy['nodes'], level):
            # parcels just outside the cell are built too, so a border edge node finds its twin
            if not inside(pc) and not any(inside(neighbour(pc, sd, level)) for sd in SIDES): continue
            P = parcels.setdefault(pc, dict(nodes={}, links=[], runs=[], touch=collections.Counter(),
                                             touch_pts=collections.defaultdict(list)))
            q = []
            lon0, lat0, w, h = parcel_geo(level, *pc)
            for p in run:
                key = quant(p, pc, level)
                ux, uy = (p[0] - lon0) / w * 2048, (p[1] - lat0) / h * 2048
                on_line = min(abs(ux), abs(ux - 2048), abs(uy), abs(uy - 2048)) < 1e-3
                if not on_line:
                    # only a cut point sits on the parcel line; a vertex or junction that merely
                    # rounds onto it stays one unit inside, or it merges with the cut node and that
                    # gets two real links, which the factory never has
                    key = (min(2046, max(1, key[0])), min(2046, max(1, key[1])))
                if not q or q[-1][0] != key: q.append((key, p))
            if len(q) < 2: continue
            P['runs'].append((q, wy))
            P['touch'][q[0][0]] += 2; P['touch'][q[-1][0]] += 2   # a run end is always a node
            P['touch_pts'][q[0][0]].append(q[0][1]); P['touch_pts'][q[-1][0]].append(q[-1][1])
            for key, p in q[1:-1]: P['touch'][key] += 1; P['touch_pts'][key].append(p)
    # two ways crossing the parcel line within one unit of each other get one key, which would
    # make a border node with two real links (the factory has none). Give
    # the second and later crossing points the nearest free keys along the line. Both parcels
    # rank the union of their points at the key and of their used keys on the line, so the
    # twins still mirror even when one side dropped a run.
    def mirror(key, sd):
        gx, gy = key
        return (0 if sd == 'right' else 2047 if sd == 'left' else gx, 0 if sd == 'top' else 2047 if sd == 'bottom' else gy)
    def on_side(key, sd):
        gx, gy = key
        return {'right': gx == 2047, 'left': gx == 0, 'top': gy == 2047, 'bottom': gy == 0}[sd]
    def corner(key): return key[0] in (0, 2047) and key[1] in (0, 2047)
    remaps = {}
    for pc, P in parcels.items():
        remap = {}
        for sd in SIDES:
            N = parcels.get(neighbour(pc, sd, level))
            mine = {k: {tuple(p) for p in v} for k, v in P['touch_pts'].items() if on_side(k, sd)}
            theirs = {mirror(k, sd): {tuple(p) for p in v} for k, v in N['touch_pts'].items() if on_side(k, sd)} if N else {}
            used = set(mine) | set(theirs)
            along = 1 if sd in ('left', 'right') else 0
            for key in sorted(used):
                if corner(key): continue
                pts = mine.get(key, set()) | theirs.get(key, set())
                for pt in sorted(pts)[1:]:
                    nk = next((k for d in range(1, 2047) for s in (d, -d)
                               for k in [key[:along] + (key[along] + s,) + key[along + 1:]]
                               if 1 <= k[along] <= 2046 and k not in used), None)
                    if nk is None: continue
                    used.add(nk)
                    if pt in mine.get(key, ()): remap[(key, pt)] = nk
        if remap: remaps[pc] = remap
    for pc, remap in remaps.items():
        P = parcels[pc]
        P['touch'] = collections.Counter(); P['touch_pts'] = collections.defaultdict(list)
        for q, wy in P['runs']:
            q[:] = [(remap.get((key, tuple(p)), key), p) for key, p in q]
            P['touch'][q[0][0]] += 2; P['touch'][q[-1][0]] += 2
            P['touch_pts'][q[0][0]].append(q[0][1]); P['touch_pts'][q[-1][0]].append(q[-1][1])
            for key, p in q[1:-1]: P['touch'][key] += 1; P['touch_pts'][key].append(p)
    # a junction that only lands on the parcel line by quantization coincidence (not a
    # real crossing, since nothing matching touches the mirrored point in the neighbour) is
    # pulled 1 unit inward, so it can't be taken for an edge-crossing node with a twin. A
    # junction that is also a real crossing is left on the line instead: the neighbour has
    # the same input point at the mirrored key, so it mirrors like any other parcel-edge
    # node rather than splitting into two disconnected copies. All nudges are decided before
    # any parcel's touch_pts is touched, since a neighbour's crossing check reads them too.
    def crosses(pc, P, key, gx, gy, sd):
        npc = neighbour(pc, sd, level)
        if npc not in parcels: return False
        tw = (0 if sd == 'right' else 2047 if sd == 'left' else gx,
              0 if sd == 'top' else 2047 if sd == 'bottom' else gy)
        mine = P['touch_pts'][key][0]
        return any(dist(p, mine) < 0.5 for p in parcels[npc]['touch_pts'].get(tw, ()))
    all_nudges = {}
    for pc, P in parcels.items():
        touch = P['touch']
        nudge = {}
        for key, n in touch.items():
            gx, gy = key
            if n > 2 and (gx in (0, 2047) or gy in (0, 2047)):
                sides = [sd for sd, ok in (('right', gx == 2047), ('left', gx == 0),
                                            ('top', gy == 2047), ('bottom', gy == 0)) if ok]
                if any(crosses(pc, P, key, gx, gy, sd) for sd in sides): continue
                nk = (1 if gx == 0 else 2046 if gx == 2047 else gx,
                      1 if gy == 0 else 2046 if gy == 2047 else gy)
                if nk != key: nudge[key] = nk
        all_nudges[pc] = nudge
    for pc, P in parcels.items():
        touch = P['touch']
        nudge = all_nudges[pc]
        if nudge:
            for k, nk in nudge.items(): touch[nk] = touch.pop(k)
            kept = []
            for q, wy in P['runs']:
                for i, (key, p) in enumerate(q):
                    if key in nudge: q[i] = (nudge[key], p)
                dedup = [q[0]]
                for item in q[1:]:
                    if item[0] != dedup[-1][0]: dedup.append(item)
                if len(dedup) >= 2: kept.append((dedup, wy))
            P['runs'] = kept
        cands = []   # (part, way, run) of every link, in run order
        for rid, (q, wy) in enumerate(P['runs']):
            start = 0
            for i in range(1, len(q)):
                if chain and touch[q[i][0]] < 2 and i - start <= MAX_INNER: continue
                seg = q[start:i + 1]; start = i
                # split a run that closes on itself, so no link joins a node to itself
                parts =[seg[:len(seg) // 2 + 1], seg[len(seg) // 2:]] if seg[0][0] == seg[-1][0] and len(seg) > 2 else [seg]
                for part in parts:
                    if part[0][0] != part[-1][0]: cands.append((part, wy, rid))
        del P['runs'], P['touch'], P['touch_pts']
        # Two links between one pair of nodes: a crescent that leaves a street and comes back
        # to it, or the two sides of a split road. The shortest stays whole and each other
        # one is cut at a middle point, which becomes a node, so both roads are on the map.
        # One with no point to cut at is the same path twice and is dropped.
        pairs = collections.defaultdict(list)
        for i, (part, wy, rid) in enumerate(cands):
            pairs[frozenset((part[0][0], part[-1][0]))].append(i)
        def length(part): return sum(dist(part[j][1], part[j + 1][1]) for j in range(len(part) - 1))
        cut_at = {}
        for ends, idx in pairs.items():
            if len(idx) == 1: continue
            short = min(idx, key=lambda i: length(cands[i][0]))
            for i in idx:
                if i == short: continue
                part = cands[i][0]
                mids = [j for j in range(1, len(part) - 1) if part[j][0] not in ends]
                cut_at[i] = mids[len(mids) // 2] if mids else None
        for i, (part, wy, rid) in enumerate(cands):
            j = cut_at.get(i, 0)
            if j is None: continue
            for piece in ([part[:j + 1], part[j:]] if j else [part]):
                for key, p in (piece[0], piece[-1]): P['nodes'].setdefault(key, dict(geo=p, edge=None))
                P['links'].append((piece[0][0], piece[-1][0], max(1.0, length(piece)), wy['id'], wy.get('name'),
                                   wy['hw'], wy.get('ref'), wy.get('maxspeed'), [k for k, _ in piece[1:-1]], rid,
                                   wy.get('dir', 0), wy.get('junction')))
        if chain: P['links'] = chain_links(P['links'])
        P['links'] = layer_order(P['links'], level)
    for pc, P in parcels.items():
        y, x, row, col = pc
        for key, nd in P['nodes'].items():
            gx, gy = key
            side = 'right' if gx == 2047 else 'left' if gx == 0 else 'top' if gy == 2047 else 'bottom' if gy == 0 else None
            if side: nd['edge'] = side
    # a node on the line with nothing at the mirrored point in an already-built neighbour was
    # never a real crossing either (typically a dead end that just happens to quantize onto
    # the line) -- nudge it off the line rather than leave it looking like a crossing with no
    # edge entry; a neighbour that was never built at all (genuinely outside this run) is left
    # alone, since that is the ordinary tile-edge case, not this defect
    for pc, P in parcels.items():
        renames = {}
        for key, nd in P['nodes'].items():
            if not nd['edge']: continue
            npc = neighbour(pc, nd['edge'], level)
            if npc not in parcels: continue
            gx, gy = key
            tw = (0 if nd['edge'] == 'right' else 2047 if nd['edge'] == 'left' else gx,
                  0 if nd['edge'] == 'top' else 2047 if nd['edge'] == 'bottom' else gy)
            if tw in parcels[npc]['nodes']: continue
            nk = (1 if gx == 0 else 2046 if gx == 2047 else gx,
                  1 if gy == 0 else 2046 if gy == 2047 else gy)
            if nk != key and nk not in P['nodes']: renames[key] = nk
        for key, nk in renames.items():
            nd = P['nodes'].pop(key); nd['edge'] = None; P['nodes'][nk] = nd
        if renames:
            P['links'] = [(renames.get(l[0], l[0]), renames.get(l[1], l[1])) + l[2:] for l in P['links']]
    for pc, P in parcels.items():
        # factory order: node class (lowest class among its links), then x, y; the unit-0 pair
        # table counts nodes per class slot, so a slot must be one contiguous index range
        ncls = {}
        for l in P['links']:
            c = fa_encode.layer_class(l[5], level)[1]
            for k in (l[0], l[1]): ncls[k] = min(c, ncls.get(k, c))
        keys = sorted(P['nodes'], key=lambda k: (ncls.get(k, 15), k[0], k[1]))
        P['order'] = keys; P['index'] = {k: i for i, k in enumerate(keys)}
        P['ring'] = not inside(pc)
    return parcels


def own(parcels):
    """The parcels of the cell itself, without the ring around it."""
    return {pc: P for pc, P in parcels.items() if not P['ring']}

def chain_links(links):
    """Reorder links so the ones drawn as one shape are consecutive; chains sharing a node, class, name and ref join."""
    chains = {}
    for l in links: chains.setdefault(l[9], []).append(l)
    def key(c): return (c[0][5], c[0][4], c[0][6])
    def flip(c): return [(l[1], l[0]) + l[2:8] + (l[8][::-1], l[9], -l[10]) + l[11:] for l in reversed(c)]
    ends = collections.defaultdict(list)
    for cid, c in chains.items(): ends[c[0][0]].append((cid, 'a')); ends[c[-1][1]].append((cid, 'b'))
    for node in sorted(ends):
        bykey = collections.defaultdict(list)
        for c, s in ends[node]: bykey[key(chains[c])].append((c, s))
        pairs = [p for p in bykey.values() if len(p) == 2 and p[0][0] != p[1][0]]
        if len(pairs) != 1: continue                         # one road, two ends, nothing ambiguous
        (ca, sa), (cb, sb) = pairs[0]
        A, B = chains[ca], chains[cb]
        if sa == 'a': A = flip(A)
        if sb == 'b': B = flip(B)
        if A[0][0] == B[-1][1]: continue                     # joining would close the loop
        chains[ca] = A + B; del chains[cb]
        ends[A[0][0]] = [(ca, 'a') if c in (ca, cb) else (c, s) for c, s in ends[A[0][0]]]
        ends[B[-1][1]] = [(ca, 'b') if c in (ca, cb) else (c, s) for c, s in ends[B[-1][1]]]
    out = []; cid = 0
    for c in chains.values():
        # a closed way (roundabout, turning circle) is two shapes, as the factory draws them
        for part in ([c[:len(c) // 2], c[len(c) // 2:]] if c[0][0] == c[-1][1] and len(c) > 1 else [c]):
            out += [l[:9] + (cid,) + l[10:] for l in part]; cid += 1
    return out

def layer_order(links, level=1):
    """Links in section-1 drawing order: by layer, chains kept whole."""
    return sorted(links, key=lambda l: fa_encode.layer_class(l[5], level)[0])

def link_ids(links, level=1):
    """The link id per link: 1..n within each class; a link is named by (class, id)."""
    n = collections.Counter(); out = []
    for l in links:
        cls = fa_encode.layer_class(l[5], level)[1]
        n[cls] += 1; out.append(n[cls])
    return out

def link_groups(links, chain=True):
    """Link positions grouped into shapes, broken before a group passes MAX_POINTS."""
    groups = []; npts = 0
    for k, l in enumerate(links):
        n = len(l[8]) + 1
        if chain and groups and links[k - 1][9] == l[9] and npts + n <= MAX_POINTS:
            groups[-1].append(k); npts += n
        else:
            groups.append([k]); npts = n + 1
    return groups

def neighbour(pc, side, level=1):
    y, x, row, col = pc
    m = DIRM[level]   # a directory spans m meshes, so a row/col wrap steps y/x by m, not 1
    dr, dc = {'top': (1, 0), 'bottom': (-1, 0), 'right': (0, 1), 'left': (0, -1)}[side]
    row += dr; col += dc
    if row < 0: row, y = 31, y - m
    if row > 31: row, y = 0, y + m
    if col < 0: col, x = 31, x - m
    if col > 31: col, x = 0, x + m
    return (y, x, row, col)

UPPER_NAMES = False   # factory labels are upper case, accents kept (1=ÅRHUSVEJ)

def label(name, ref):
    """The shipped label text: `1=NAME;2=REF`, either part alone when the other is missing."""
    if name and UPPER_NAMES: name = name.upper()
    parts = ([f'1={name}'] if name else []) + ([f'2={ref}'] if ref else [])
    return ';'.join(parts)

def map_turns(parcels, turns, level=1):
    """{parcel: [(node index, from link index, to link index)]} and a Counter of drops."""
    out = {}; stats = collections.Counter(); inc = {}
    for pc, P in parcels.items():
        m = {}
        for li, l in enumerate(P['links']):
            for key in (l[0], l[1]): m.setdefault(P['index'][key], []).append((li, l[3]))
        inc[pc] = m
    for r in turns:
        lon, lat = r['via']
        pc = parcel_of(lon, lat, level)
        P = parcels.get(pc)
        if P is None: stats['via outside this cell'] += 1; continue
        nd = P['index'].get(quant((lon, lat), pc, level))
        if nd is None: stats['via is not a graph node'] += 1; continue
        cand = inc[pc].get(nd, [])
        f = sorted({li for li, w in cand if w == r['from']})
        t = sorted({li for li, w in cand if w == r['to']})
        if not f or not t: stats['from or to link not at the node'] += 1; continue
        if len(f) > 1 or len(t) > 1: stats['way runs through the node, ambiguous'] += 1; continue
        out.setdefault(pc, []).append((nd, f[0], t[0])); stats['mapped'] += 1
    return out, stats

def chains_of(links, idx, chain=True, ids=None, groups=None):
    """Shapes as [(link id, from index, to index, inner points)] chains over link_groups."""
    if groups is None: groups = link_groups(links, chain)
    return [[(ids[k] if ids else k + 1, idx[links[k][0]], idx[links[k][1]], links[k][8]) for k in g] for g in groups]

def heading(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])

def turn_deg(h_in, h_out):
    """Signed turn from one heading to the next, -180..180, left positive."""
    d = math.degrees(h_out - h_in)
    return d - 360 if d > 180 else d + 360 if d <= -180 else d

def next_point(l, node, idx):
    """The point of link l beside the node that is one of its ends."""
    inner = l[8]
    if idx[l[0]] == node: return inner[0] if inner else l[1]
    return inner[-1] if inner else l[0]

MAX_DEPARTURE = 100   # degrees: a ramp leaving at more than this is not an exit from that approach

def find_signposts(L, nodes, idx, groups, signs, upper=False):
    """Signposts of one level-1 parcel, before the record is laid out.

    A sign hangs on a ramp (any *_link way with destination tags) at the node where it leaves
    a road that is not a ramp; the approach is the incoming road link whose heading the ramp
    departs from by the smallest turn. Returns [dict(exit=(chain, occurrence),
    approach=(chain, occurrence), bits, text)], where occurrence n of a chain is the start
    node of its link n and len(chain) its last node (fa_encode.shape_words)."""
    pos = {k: (i, n) for i, g in enumerate(groups) for n, k in enumerate(g)}
    inc = collections.defaultdict(list)
    for k, l in enumerate(L):
        inc[idx[l[0]]].append(k); inc[idx[l[1]]].append(k)
    def roads_at(n, k): return [kk for kk in inc[n] if kk != k and not L[kk][5].endswith('_link')]
    out = []
    for k, l in enumerate(L):
        if not l[5].endswith('_link') or not signs.get(l[3]): continue
        text = sec4_write.sign_text(signs[l[3]], upper)
        if text is None: continue   # blank destination tags name nothing; skip at the source
        a, b = idx[l[0]], idx[l[1]]
        if l[10] > 0: J, fwd = a, True
        elif l[10] < 0: J, fwd = b, False
        else:   # a two-way ramp: the end that meets a road, when only one does
            if pos[k][1] != 0: continue   # not the chain's first link: its far end is the merge, not a departure
            ends = [(n, f) for n, f in ((a, True), (b, False)) if roads_at(n, k)]
            if len(ends) != 1: continue
            J, fwd = ends[0]
        h_out = heading(nodes[J], next_point(l, J, idx))
        best = None
        for kk in roads_at(J, k):
            la = L[kk]
            if la[10] > 0 and idx[la[1]] != J: continue
            if la[10] < 0 and idx[la[0]] != J: continue
            into_fwd = idx[la[1]] == J           # travelling la in storage order ends at J
            t = abs(turn_deg(heading(next_point(la, J, idx), nodes[J]), h_out))
            if t <= MAX_DEPARTURE and (best is None or t < best[0]): best = (t, kk, into_fwd)
        if best is None: continue
        _, kk, into_fwd = best
        ie, ne = pos[k]; ia, na = pos[kk]
        out.append(dict(exit=(ie, ne if fwd else ne + 1), approach=(ia, na if into_fwd else na + 1),
                        bits=sec4_write.APPROACH_FWD if into_fwd else sec4_write.APPROACH_BWD, text=text))
    return out

def signpost_subs(found, occ_words):
    """Section-4 sub-records from find_signposts' list once the record is laid out: one per
    exit node occurrence, in node-key order, with (flag, ref) naming the approach link's far
    node and the exit code counting the signs of one approach."""
    per_node = collections.defaultdict(list)
    for s in found: per_node[s['exit']].append(s)
    per_approach = collections.Counter()
    subs = []
    for (ie, ne), lst in per_node.items():
        entries = []
        for s in lst:
            ia, na = s['approach']
            code = sec4_write.EXIT_CODES[min(per_approach[s['approach']], len(sec4_write.EXIT_CODES) - 1)]
            per_approach[s['approach']] += 1
            entries.append(dict(dir=code | s['bits'], pairs=[(sec4_write.FLAG, occ_words[ia][na] - 22)], text=s['text']))
        subs.append(dict(key=occ_words[ie][ne] - 22, groups=[dict(hdr=sec4_write.GROUP_HDR, entries=entries)]))
    subs.sort(key=lambda s: s['key'])
    return subs

def node_ways(P):
    """{node key: way ids of its links}, cached on the parcel."""
    if 'node_ways' not in P:
        w = collections.defaultdict(set)
        for l in P['links']: w[l[0]].add(l[3]); w[l[1]].add(l[3])
        P['node_ways'] = {k: frozenset(v) for k, v in w.items()}
    return P['node_ways']

def l1_twin(geo, pc, l1_index):
    """(cell word, L1 index) of geo's L1 twin inside parcel pc's 4x4 grid, or None.
    L1's own (y0, row) is unsnapped (DIRM 1), already an exact absolute row at the
    level-1 scale (32 per mesh unit); L2's is snapped to a multiple of DIRM[2]=4 mesh
    units, 8 per mesh unit. Comparing the two absolute addresses gives the L1 twin's
    cell inside pc's 4x4 grid."""
    abs_row2, abs_col2 = pc[0] * 32 + pc[2] * 4, pc[1] * 32 + pc[3] * 4
    for (y1, x1, row1, col1), idx1, *_ in l1_index.get(tuple(geo), ()):
        subrow = y1 * 32 + row1 - abs_row2
        subcol = x1 * 32 + col1 - abs_col2
        if 0 <= subrow < 4 and 0 <= subcol < 4:
            return (subrow << 14) | (subcol << 6), idx1
    return None

NEAR_EDGE_M = 60    # an L2 edge node and its L1 twin cut the parcel line from differently simplified polylines
NEAR_INNER_M = 10

def l1_nearest(geo, side, pc, l1_index, ways=frozenset(), limit=None):
    """(cell word, L1 index) of the L1 node nearest to geo inside parcel pc's 4x4 grid, when no L1
    node sits at exactly geo. A parcel-edge node must pair with an edge node on the same side: the
    firmware reads the border test's x,y off that L1 node (0x3545dc), so an inner one loses the
    border mode that keeps edge triples away from the own record's table."""
    near = l1_nearest.cache.get(id(l1_index))
    if near is None:
        near = {}
        for g, infos in l1_index.items():
            for pc1, idx, key, w in infos: near.setdefault(pc1, []).append((g, idx, key, w))
        l1_nearest.cache = {id(l1_index): near}
    y2, x2, row2, col2 = pc
    base_row, base_col = y2 * 32 + row2 * 4, x2 * 32 + col2 * 4
    rows = {'bottom': (0,), 'top': (3,)}.get(side, range(4))
    cols = {'left': (0,), 'right': (3,)}.get(side, range(4))
    want = {'bottom': lambda k: k[1] == 0, 'top': lambda k: k[1] == 2047,
            'left': lambda k: k[0] == 0, 'right': lambda k: k[0] == 2047}.get(side, lambda k: True)
    best = None
    for r in rows:
        for c in cols:
            ar, ac = base_row + r, base_col + c
            for g, idx, key, w in near.get((ar // 32, ac // 32, ar % 32, ac % 32), ()):
                if not want(key): continue
                d = dist(g, geo)
                if d > (limit if limit is not None else NEAR_EDGE_M if side else NEAR_INNER_M): continue
                rank = (not (w & ways), d)   # same road first, then distance
                if best is None or rank < best[0]: best = (rank, (r << 14) | (c << 6), idx)
    if best is None: return None
    l1_nearest.max_m = max(l1_nearest.max_m, best[0][1])
    l1_nearest.other_way += best[0][0]
    return best[1], best[2]
l1_nearest.cache = {}; l1_nearest.max_m = 0.0; l1_nearest.other_way = 0

def l1_next_node(pair, pc, l1_adj, geo_from, geo_to):
    """The L1 neighbour of an L2 node's twin that heads the way the L2 link does."""
    if pair is None or l1_adj is None: return None
    cell, idx = pair
    # L1 directories are one mesh unit, L2's four (l1_twin): rebase the absolute L1 address
    ar, ac = pc[0] * 32 + pc[2] * 4 + (cell >> 14), pc[1] * 32 + pc[3] * 4 + ((cell >> 6) & 3)
    nbrs = l1_adj.get((ar // 32, ac // 32, ar % 32, ac % 32), {}).get(idx)
    if not nbrs: return None
    k = math.cos(math.radians(geo_from[1]))
    dx, dy = (geo_to[0] - geo_from[0]) * k, geo_to[1] - geo_from[1]
    def cosine(g):
        ex, ey = (g[0] - geo_from[0]) * k, g[1] - geo_from[1]
        n = math.hypot(dx, dy) * math.hypot(ex, ey)
        return (dx * ex + dy * ey) / n if n else -2
    return max(nbrs, key=lambda n: cosine(n[1]))[0]

def encode_parcels(parcels, level=1, no_names=False, chain=True, regs=None, oneway_regs=True, l2_geo=None,
                   l1_index=None, signs=None, l1_adj=None, l2_hw=None):
    out = {}; encode_parcels.regs_written = encode_parcels.regs_dropped = encode_parcels.no_twin = 0
    encode_parcels.l1_missing = encode_parcels.l1_mismatch = encode_parcels.l1_near = encode_parcels.l1_next_missing = 0
    encode_parcels.unpaired = []
    encode_parcels.index = {}; encode_parcels.signposts = {}
    for pc, P in parcels.items():
        if P.get('ring'): continue
        keys = P['order']; idx = P['index']
        nodes = list(keys)
        links = [(idx[a], idx[b], ln) for a, b, ln, *_ in P['links']]
        ids = link_ids(P['links'], level)
        edges = {}
        for key, nd in P['nodes'].items():
            if not nd['edge']: continue
            npc = neighbour(pc, nd['edge'], level); twin = None
            if npc in parcels:
                gx, gy = key
                tw = (0 if nd['edge'] == 'right' else 2047 if nd['edge'] == 'left' else gx, 0 if nd['edge'] == 'top' else 2047 if nd['edge'] == 'bottom' else gy)
                twin = parcels[npc]['index'].get(tw)   # a run that only grazes the neighbour leaves no twin
            if twin is None: encode_parcels.no_twin += 1
            twin_child = None
            if twin is not None and level == 2 and l1_index is not None:
                tgeo = parcels[npc]['nodes'][tw]['geo']
                tpair = l1_twin(tgeo, npc, l1_index) or l1_nearest(tgeo, parcels[npc]['nodes'][tw]['edge'], npc, l1_index,
                                                                   node_ways(parcels[npc]).get(tw, frozenset()))
                twin_child = tpair[1] if tpair else None
            if twin is not None: edges[idx[key]] = (SIDES[nd['edge']], twin, twin_child)
        # a level-1 node also present at level 2 carries 0x2000, unrelated to the parcel-edge twin above
        l2_flags = [tuple(P['nodes'][k]['geo']) in l2_geo for k in keys] if l2_geo is not None else None
        l1_pair = None
        if level == 2 and l1_index is not None:
            l1_pair = []
            for key in keys:
                pair = l1_twin(P['nodes'][key]['geo'], pc, l1_index)
                if pair is None:
                    pair = l1_nearest(P['nodes'][key]['geo'], P['nodes'][key]['edge'], pc, l1_index,
                                      node_ways(P).get(key, frozenset()))
                    if pair is not None: encode_parcels.l1_near += 1
                    elif l1_index.get(tuple(P['nodes'][key]['geo'])) is None:
                        encode_parcels.l1_missing += 1
                        encode_parcels.unpaired.append((pc, key, P['nodes'][key]['edge'], P['nodes'][key]['geo']))
                        # no L1 node near: the block must still name a real L1 node, since the
                        # descent dereferences whatever it names; take the nearest at any distance
                        pair = l1_nearest(P['nodes'][key]['geo'], None, pc, l1_index, limit=1e9)
                    else: encode_parcels.l1_mismatch += 1
                l1_pair.append(pair)
        l1_next = None
        if l1_pair is not None and l1_adj is not None:
            l1_next = []
            for a, b, *_ in P['links']:
                ga, gb = P['nodes'][a]['geo'], P['nodes'][b]['geo']
                l1_next.append((l1_next_node(l1_pair[idx[a]], pc, l1_adj, ga, gb),
                                l1_next_node(l1_pair[idx[b]], pc, l1_adj, gb, ga)))
            encode_parcels.l1_next_missing += sum(x is None for pair in l1_next for x in pair)
        # the factory marks a roundabout ring's own links (not its approach/exit spokes) with
        # w4's top 3 bits all set, both ends alike, at both L1 and L2 -- measured on the Koln
        # and UK factory tiles, never on a plain one-way link's entries
        link_w4 = [0x7000 if l[11] in ('roundabout', 'circular') else 0 for l in P['links']]
        # w4 bits 1-2 (KIWI link direction, 7.A.8): 0 on a level-1-only link, 1-3 on a link that an
        # upper-level link integrates. Factory B58R060R: set on 190,880/190,880 L1 links that an L2
        # link expands to, clear on 95,412/95,412 side links at the same nodes and on every class
        # 8/9/12 link; value 1 on every class 5 and 7 link, 1-3 by class on dual carriageways.
        integrated = None
        if level == 1 and l2_hw is not None:
            integrated = [l[5] in l2_hw for l in P['links']]
            link_w4 = [w | (0x2 if g else 0) for w, g in zip(link_w4, integrated)]
        out[pc] = rd_encode.build_record_edges(level, pc[0], pc[1], pc[2], pc[3], nodes, links, edges,
                                               integrated=integrated,
                                               classes=[l[5] for l in P['links']],
                                               speeds=[rd_encode.speed_of(l[5], l[7], SPEED_UNIT) for l in P['links']],
                                               regs=(regs or {}).get(pc), country=COUNTRY,
                                               dirs=[l[10] for l in P['links']], ids=ids, link_w4=link_w4,
                                               oneway_regs=oneway_regs, l2_twin=l2_flags, l1_pair=l1_pair,
                                               l1_next=l1_next)
        encode_parcels.regs_written += rd_encode.build_record_edges.regs_written
        encode_parcels.regs_dropped += rd_encode.build_record_edges.regs_dropped
        groups = link_groups(P['links'], chain)
        chains = chains_of(P['links'], idx, chain, ids, groups)
        L = P['links']
        hws = [L[g[0]][5] for g in groups]
        label_of = [label(L[g[0]][4], L[g[0]][6]) for g in groups]
        labels = []
        for n in label_of:
            if n and n not in labels: labels.append(n)
        base = KEEP_BASE.get((level,) + pc, (b'', 0)) if KEEP_BASE is not None else (b'', 0)
        _, offs = fa_encode.names_section(labels, [[0] * 9] * len(chains), base)
        name_offs = [0xffff] * len(chains) if no_names else [offs.get(n, 0xffff) for n in label_of]
        # a ferry link is two-way, but style_word's oneway test would still clear its bit 12
        styles = [[FMT.ferry_style if L[k][5] == 'ferry' else
                   fa_encode.style_word(fa_encode.speed_limit(L[k][7], SPEED_UNIT), L[k][10] != 0) for k in g]
                  for g in groups]
        dirs = [[L[k][10] for k in g] for g in groups]
        rings = [all(L[k][11] in ('roundabout', 'circular') for k in g) for g in groups]
        area = area_of(level, pc) if AREA_BASE is not None else None
        found = find_signposts(L, nodes, idx, groups, signs, UPPER_NAMES) if signs and level == 1 else []
        gs = None
        if found:
            # a first layout with G placeholders fixes the node offsets; the sub-record order
            # those give fixes each G
            gs = {}
            for s in found: gs.setdefault(s['exit'][0], {})[s['exit'][1]] = 0
            fa_encode.build_record(level, pc[0], pc[1], pc[2], pc[3], nodes, chains, name_offs=name_offs, hws=hws,
                                   styles=styles, country=COUNTRY, dirs=dirs, gs=gs, rings=rings, area=area)
            occ = fa_encode.occurrence_words(fa_encode.build_record.last_index)
            subs = signpost_subs(found, occ)
            L_of_key = dict(zip((s['key'] for s in subs), sec4_write.sub_offsets(subs)))
            gs = {i: {n: L_of_key[occ[i][n] - 22] for n in o} for i, o in gs.items()}
            encode_parcels.signposts[pc] = subs
        fa = fa_encode.build_record(level, pc[0], pc[1], pc[2], pc[3], nodes, chains, name_offs=name_offs, hws=hws,
                                    styles=styles, country=COUNTRY, dirs=dirs, gs=gs, rings=rings, area=area)
        encode_parcels.index[pc] = shape_index_record(level, pc, fa_encode.build_record.last_index, groups, ids, L,
                                                      label_of, no_names)
        section, _ = fa_encode.names_section(labels, fa_encode.build_record.last_items, base)
        out[pc] = out[pc] + fa + (None if no_names else section,)
    return out

def shape_index_record(level, pc, ix, groups, ids, links, label_of, no_names):
    """One record of the shape index (shape_index.py): where each chain's points landed."""
    lon0, lat0, w, h = parcel_geo(level, *pc)
    def geo(k): return [round(lon0 + k[0] / 2048 * w, 7), round(lat0 + k[1] / 2048 * h, 7)]
    shapes = []
    for S in ix['shapes']:
        g = groups[S['chain']]
        keys = [links[g[0]][0]] + [key for k in g for key in links[k][8] + [links[k][1]]]
        assert len(keys) == len(S['points'])
        lab = '' if no_names else (label_of[S['chain']] or '')
        shapes.append(dict(name=links[g[0]][4], label=lab, layer=S['layer'], cls=S['cls'], at=S['at'],
                           links=[dict(id=ids[k], way=links[k][3]) for k in g],
                           points=[dict(word=wd, link=lid, role=role, lon=pt[0], lat=pt[1])
                                   for (wd, lid, role), pt in zip(S['points'], map(geo, keys))]))
    y, x, row, col = pc
    return dict(code=list(ix['code']), unit=ix['unit'], mesh=[y, x], row=row, col=col,
                box=[lon0, lat0, w, h], shapes=shapes)

def simplify(pts, tol, junctions=frozenset()):
    """Douglas-Peucker in metres; endpoints and every shared-way junction point are kept."""
    if len(pts) < 3: return pts
    lat = math.radians(pts[0][1]); kx, ky = 111320 * math.cos(lat), 111320
    def d2(p, a, b):
        ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky; bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
        L = bx * bx + by * by
        t = 0 if L == 0 else max(0, min(1, -(ax * bx + ay * by) / L))
        return (ax + bx * t) ** 2 + (ay + by * t) ** 2
    keep = sorted({0, len(pts) - 1} | {i for i, p in enumerate(pts) if tuple(p) in junctions})
    stack = list(zip(keep, keep[1:]))
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

def other_labels(b):
    """(bytes, count) of the entries that are not road labels. A road label is tied to a road
    string by its offset; the other kinds carry their own position and nothing points at them."""
    p = 0; out = b''; n = 0
    while p + 10 <= len(b):
        typ, flags = struct.unpack_from('>HH', b, p); q = p + 8 + (2 if flags & 0x4000 else 0)
        end = q + 2 + struct.unpack_from('>H', b, q)[0]
        if typ != fa_encode.ROAD_LABEL: out += b[p:end]; n += 1
        p = end
    return out, n

def count_labels(b):
    p = 0; n = 0
    while p + 10 <= len(b):
        typ, flags = struct.unpack_from('>HH', b, p); q = p + 8 + (2 if flags & 0x4000 else 0)
        ln = struct.unpack_from('>H', b, q)[0]; p = q + 2 + ln; n += 1
    return n

FAST_CLASSES = {'motorway', 'trunk', 'motorway_link', 'trunk_link', 'primary_link',
                'secondary_link', 'tertiary_link'}

def derive_dir(ways):
    """Fill in a missing dir with osm_roads.oneway_dir, the same rule the extractors use, so a
    way missing dir is not silently flattened to two-way; warns when a fast way has neither field."""
    bare = [w for w in ways if 'dir' not in w and 'oneway' not in w and 'junction' not in w]
    fast = [w for w in bare if w['hw'] in FAST_CLASSES]
    if fast:
        print('WARNING: %d ways have neither dir, oneway nor junction, %d of them motorway/trunk/*_link '
              '(treated as two-way)' % (len(bare), len(fast)), file=sys.stderr)
    for w in ways:
        if 'dir' not in w:
            w['dir'] = osm_roads.oneway_dir(w.get('oneway'), w.get('junction'), w['hw'])

def main():
    argv = sys.argv[1:]
    global COUNTRY
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    if '--country' in argv:
        i = argv.index('--country'); COUNTRY = int(argv[i + 1]); del argv[i:i + 2]
    if not COUNTRY:
        raise SystemExit('no country: set country in the profile or pass --country N')
    skip, tol, l2 = set(P.skip), P.simplify_metres, P.l2_classes
    no_l2_graph, no_l2_names = not P.l2_graph, False
    if '--skip' in argv:
        i = argv.index('--skip'); skip = set(argv[i + 1].split(',')); del argv[i:i + 2]
    if '--simplify' in argv:
        i = argv.index('--simplify'); tol = float(argv[i + 1]); del argv[i:i + 2]
    if '--l2-simplify' in argv:
        i = argv.index('--l2-simplify'); P.l2_simplify_metres = float(argv[i + 1]); del argv[i:i + 2]
    if '--no-l2-graph' in argv: argv.remove('--no-l2-graph'); no_l2_graph = True
    if '--l2-graph' in argv: argv.remove('--l2-graph'); no_l2_graph = False
    if '--no-l2-names' in argv: argv.remove('--no-l2-names'); no_l2_names = True
    no_chain = '--no-chain' in argv
    if no_chain: argv.remove('--no-chain')
    oneway_regs = '--no-oneway-regs' not in argv
    if not oneway_regs: argv.remove('--no-oneway-regs')
    if '--l2-classes' in argv:
        i = argv.index('--l2-classes'); l2 = clarion_profile.l2_classes(argv[i + 1]); del argv[i:i + 2]
    global UPPER_NAMES
    if '--upper-names' in argv: argv.remove('--upper-names'); UPPER_NAMES = True
    index_path = None
    if '--shape-index' in argv:
        i = argv.index('--shape-index'); index_path = argv[i + 1]; del argv[i:i + 2]
    turns = []
    if '--turn-restrictions' in argv:
        i = argv.index('--turn-restrictions'); turns = json.load(open(argv[i + 1])); del argv[i:i + 2]
    keep_names = '--keep-names' in argv
    if keep_names: argv.remove('--keep-names')
    report = []
    signs_path = None
    if '--signposts' in argv:
        i = argv.index('--signposts'); signs_path = argv[i + 1]; del argv[i:i + 2]
    src, dest, cell = argv[:3]
    rpath = argv[3] if len(argv) > 3 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    ways = [w for w in json.load(open(rpath)) if w['hw'] not in skip]
    derive_dir(ways)
    signs = {w['id']: w['dest'] for w in ways if w.get('dest')} if signs_path else None
    if signs_path and not signs:
        print('WARNING: --signposts but no way carries destination tags; rebuild roads.json with osm_roads.py', file=sys.stderr)
    if tol:
        shared = collections.Counter(tuple(p) for w in ways for p in w['nodes'])
        junctions = {p for p, n in shared.items() if n > 1}
        for w in ways: w['nodes'] = simplify(w['nodes'], tol, junctions)
    moved = off_parcel_lines(ways)
    for r in turns:
        if tuple(r['via']) in moved: r['via'] = moved[tuple(r['via'])]
    ways2 = [dict(w) for w in ways if w['hw'] in l2]
    tol2 = P.l2_simplify_metres if P.l2_simplify_metres is not None else tol
    if tol2:
        shared = collections.Counter(tuple(p) for w in ways2 for p in w['nodes'])
        junctions = {p for p, n in shared.items() if n > 1}
        for w in ways2: w['nodes'] = simplify(w['nodes'], tol2, junctions)
    d = open(src, 'rb').read(); t = clarion_tile.read_tile(d)
    sec1 = [s for s in t['sections'] if s['id'] == 1][0]; sec2 = [s for s in t['sections'] if s['id'] == 2][0]
    if FMT.area_items:
        global AREA_BASE
        AREA_BASE = factory_areas(sec1)
        report.append(f'area codes read from {len(AREA_BASE)} parcels of the source tile')
    if keep_names:
        global KEEP_BASE
        KEEP_BASE, odd = factory_labels(sec1)
        report.append(f'--keep-names: labels kept in {len(KEEP_BASE)} parcels, {odd} names sections '
                      f'in an unknown form left to be replaced')
    # level 2 is needed before level 1 is encoded, so a level-1 node at the same point can carry 0x2000
    l2_parcels = build_graphs(ways2, cell, 2, not no_chain)
    l2_geo = {tuple(nd['geo']) for P in l2_parcels.values() for nd in P['nodes'].values()}
    boxes = {}; l1_index = None; l1_adj = None
    for level, subset in ((1, ways), (2, ways2)):
        chain = not no_chain
        parcels = l2_parcels if level == 2 else build_graphs(subset, cell, level, chain)
        if level == 1:
            # the level-1 node an L2 node pairs with, by exact geo match: (parcel, index in that record)
            l1_index = {}; l1_adj = {}
            for pc, P in parcels.items():
                if P['ring']: continue
                ways_at = node_ways(P)
                adj = l1_adj.setdefault(pc, {})
                for a, b, *_ in P['links']:
                    adj.setdefault(P['index'][a], []).append((P['index'][b], P['nodes'][b]['geo']))
                    adj.setdefault(P['index'][b], []).append((P['index'][a], P['nodes'][a]['geo']))
                # the parcel-edge twin, same point: chosen only when no real link heads the right way
                for key, nd in P['nodes'].items():
                    if not nd['edge']: continue
                    npc = neighbour(pc, nd['edge'], 1); gx, gy = key
                    tw = (0 if nd['edge'] == 'right' else 2047 if nd['edge'] == 'left' else gx,
                          0 if nd['edge'] == 'top' else 2047 if nd['edge'] == 'bottom' else gy)
                    twin = parcels[npc]['index'].get(tw) if npc in parcels else None
                    if twin is not None: adj.setdefault(P['index'][key], []).append((twin, nd['geo']))
                for key, nd in P['nodes'].items():
                    # the two edge nodes either side of a parcel line share one geo
                    l1_index.setdefault(tuple(nd['geo']), []).append((pc, P['index'][key], key, ways_at.get(key, frozenset())))
        regs, tstats = map_turns(parcels, turns, level) if (turns and level == 1) else ({}, None)
        recs = encode_parcels(parcels, level, no_names=(level == 2 and no_l2_names), chain=chain, regs=regs,
                              oneway_regs=oneway_regs, l2_geo=(l2_geo if level == 1 else None),
                              l1_index=(l1_index if level == 2 else None), signs=signs,
                              l1_adj=(l1_adj if level == 2 else None), l2_hw=(set(l2) if level == 1 else None))
        if level == 1:
            index = [encode_parcels.index[pc] for pc in sorted(encode_parcels.index)]
            signposts = [dict(y=pc[0], x=pc[1], row=pc[2], col=pc[3], subs=subs)
                         for pc, subs in sorted(encode_parcels.signposts.items())]
        parcels = own(parcels)
        shapes = [c for P in parcels.values() for c in chains_of(P['links'], P['index'], chain)]
        lv = [L for L in sec2['levels'] if L['level'] == level][0]; lv['dirs'] = []
        per_dir = {}
        for (y, x, row, col), (plain, comp, fplain, fcomp, _) in recs.items():
            per_dir.setdefault((y, x), {}).setdefault(row, {})[col] = (plain, comp)
        for (y, x), rows in sorted(per_dir.items()):
            if not (level == 2 and no_l2_graph): lv['dirs'].append(rd_encode.make_dir(level, y, x, rows, FMT))
        if not lv['dirs'] and not (level == 2 and no_l2_graph):
            lv['dirs'].append(rd_encode.make_dir(level, *tile_cell(cell), {}, FMT))   # a tile declaring none hangs the first draw
        bydir = {(D['y'], (D['x'] + FMT.x_base) & 0xffff): D for L in sec1['levels'] if L['level'] == level for D in L['dirs']}
        was_stale = merge_names.stale_replaced
        placed = place_shapes(recs, bydir)
        if merge_names.stale_replaced > was_stale:
            report.append(f'level {level}: {merge_names.stale_replaced - was_stale} stale road labels '
                          f'from an earlier pass replaced, not kept')
        PW, PH = PSIZE[level]
        if recs and lv['dirs']:
            lats = [parcel_geo(level, *pc)[1] for pc in recs] + [parcel_geo(level, *pc)[1] + PH for pc in recs]
            lons = [parcel_geo(level, *pc)[0] for pc in recs] + [parcel_geo(level, *pc)[0] + PW for pc in recs]
            boxes[level] = (math.ceil(max(lats) * 60), math.floor(min(lats) * 60),
                            math.floor(min(lons) * 60), math.ceil(max(lons) * 60))
        counts = sorted(len(Q['nodes']) for Q in parcels.values()) or [0]
        if level == 1 and moved:
            report.append(f'level 1: {len(moved)} junctions on a parcel line moved one level-2 unit inside the parcel east/north of it')
        if encode_parcels.no_twin:
            report.append(f'level {level}: {encode_parcels.no_twin} edge nodes with no node at the mirrored point '
                          f'across the edge, left without an edge entry')
        if level == 2:
            report.append(f'level 2: link ends without an L1 next node {encode_parcels.l1_next_missing} (w4 falls back to the far L2 index)')
            report.append(f'level 2: L1 twins by nearest node {encode_parcels.l1_near} (max {l1_nearest.max_m:.1f} m, '
                          f'{l1_nearest.other_way} on another road), '
                          f'no L1 node at all {encode_parcels.l1_missing}, exact twin outside the parcel {encode_parcels.l1_mismatch}')
            for pc, key, side, g in encode_parcels.unpaired[:12]:
                report.append(f'  unpaired L2 node {pc} key {key} side {side} at {g[1]:.6f},{g[0]:.6f}')
        report.append(f'level {level}: {len(recs)} parcels in {len(lv["dirs"])} dirs, '
                      f'{sum(len(Q["nodes"]) for Q in parcels.values())} nodes, shapes placed {placed}, '
                      f'nodes/parcel median {counts[len(counts)//2]} max {counts[-1]}')
        if shapes:
            pts = sorted(1 + sum(len(inner) + 1 for _, _, _, inner in c) for c in shapes)
            multi = sum(1 for c in shapes if len(c) > 1)
            report.append(f'  shapes {len(shapes)}: points/shape median {pts[len(pts)//2]} '
                          f'mean {sum(pts)/len(pts):.2f} max {pts[-1]}, '
                          f'multi-link {100 * multi / len(shapes):.1f}%')
        if tstats is not None:
            report.append('  turn restrictions: %d of %d written, %d dropped in the encoder; %s'
                          % (encode_parcels.regs_written, len(turns), encode_parcels.regs_dropped,
                             ', '.join('%s %d' % kv for kv in tstats.most_common()) or 'none mapped'))
    nw, _, _ = clarion_tile.table_words(sec2); sec2['count'] = -(-2 * nw // 256)
    hdr = bytearray(t['header'])
    p = 0x1ac + 2; groups = hdr[p - 2] << 8 | hdr[p - 1]
    for _ in range(groups):
        sid, cnt = hdr[p], hdr[p + 1]; p += 2
        for _ in range(cnt):
            if sid == 2 and hdr[p] in (1, 2):
                struct.pack_into('>H', hdr, p + 2, len([L for L in sec2['levels'] if L['level'] == hdr[p]][0]['dirs']))
            p += 4
    # section 2's box rows are replaced; a group after it, such as section 4's, keeps its rows
    new = bytes(hdr[0x2ac:0x2ae]); p = 0x2ae; seen = False
    for _ in range(struct.unpack_from('>H', hdr, 0x2ac)[0]):
        sid, n = hdr[p], hdr[p + 1]
        if sid == 2:
            new += bytes([2, len(boxes)]); seen = True
            for level in sorted(boxes, reverse=True): new += struct.pack('>BB4h', level, 0, *boxes[level])
        else:
            new += bytes(hdr[p:p + 2 + 10 * n])
        p += 2 + 10 * n
    assert seen, 'the header has no box group for section 2'
    assert 0x2ac + len(new) <= 0x3ac, 'box table runs into the constant at 0x3ac'
    end = max(p, 0x2ac + 0x30, 0x2ac + len(new))
    hdr[0x2ac:end] = new + bytes([FMT.table_fill]) * (end - 0x2ac - len(new))
    t['header'] = bytes(hdr)
    out = clarion_tile.write_tile(t, clarion_tile.default_order(t)); open(dest, 'wb').write(out)
    print(f'wrote {dest} {len(out)} bytes; ways used {len(ways)}')
    for r in report: print('  ' + r)
    if signs_path:
        with open(signs_path, 'w') as f:
            json.dump(dict(cell=cell, country=COUNTRY, parcels=signposts), f, ensure_ascii=False, indent=1)
        print(f'  signposts {signs_path}: {sum(len(P["subs"]) for P in signposts)} sub-records in {len(signposts)} parcels; '
              f'now run sec4_write.py --signposts {signs_path}')
    if index_path:
        side = dict(cell=cell, level=1, records=index)
        bad = shape_index.verify(side, out)   # read back off the written tile, not the encoder
        with open(index_path, 'w') as f: json.dump(side, f, ensure_ascii=False, indent=1)
        print(f'  shape index {index_path}: {len(index)} records, '
              f'{sum(len(R["shapes"]) for R in index)} shapes, checked against the tile: '
              + ('ok' if not bad else f'{len(bad)} PROBLEMS'))
        for b in bad[:20]: print('    ' + b)
        if bad: sys.exit(1)


def merge_names(plain, section):
    """A background record with its road names section replaced (never kept: the names slot
    only ever holds a prior rd_build pass's own labels, so keeping them would leave stale,
    dead labels behind on a second run over the same tile)."""
    r = clarion_bg.parse_record(plain)
    old = r['sections'].get('names')
    if old:
        sub_off = struct.unpack_from('>H', old, 8)[0]
        stale = labels_unpadded(old[24:4 * sub_off] if sub_off != 0xffff else old[24:])
        n = count_labels(stale)
        if n: merge_names.stale_replaced += n
    r['sections']['names'] = section
    return clarion_bg.build_record(r)
merge_names.stale_replaced = 0


def factory_labels(sec1):
    """{(level, y, x, row, col): (label bytes, count)} from every background record's names section, for
    --keep-names; and how many sections had a form this does not read. The factory's road labels
    are left out: their roads are gone, and a rebuilt parcel gets its own."""
    out, odd = {}, 0
    for L in sec1['levels']:
        for D in L['dirs']:
            x = (D['x'] + FMT.x_base) & 0xffff
            for row, s in D['slots'].items():
                for r in s['recs']:
                    if r['k'] % 2 == 0: continue
                    p = FMT.inflate(r['raw']); w = struct.unpack_from('>22H', p)
                    if w[11] == 0xffff: continue
                    sec = p[4 * w[11]:4 * (w[11] + w[12])]; h = struct.unpack_from('>12H', sec)
                    lab = labels_unpadded(sec[24:4 * h[4]] if h[4] != 0xffff else sec[24:])
                    n = count_labels(lab)
                    if h[0] == 0xffff and not lab: n = 0
                    elif h[0] != 6 or n != h[1]: odd += 1; continue
                    out[(L['level'], D['y'], x, row, (r['k'] - 1) // 2)] = other_labels(lab)
    return out, odd


def factory_areas(sec1):
    """{(level, y, x, row, col): (a, b, c)}: the area codes most of a parcel's names items
    carry for our country. An item is a length-prefixed run of words, nine for one area."""
    out = {}
    for L in sec1['levels']:
        for D in L['dirs']:
            x = (D['x'] + FMT.x_base) & 0xffff
            for row, s in D['slots'].items():
                for r in s['recs']:
                    if r['k'] % 2 == 0: continue
                    p = FMT.inflate(r['raw']); w = struct.unpack_from('>22H', p)
                    if w[11] == 0xffff: continue
                    sec = p[4 * w[11]:4 * (w[11] + w[12])]; h = struct.unpack_from('>12H', sec)
                    if h[4] == 0xffff: continue
                    sub = sec[4 * h[4]:]; o = 16; seen = collections.Counter()
                    for _ in range(struct.unpack_from('>H', sub, 2)[0]):
                        if o + 18 > len(sub): break
                        it = struct.unpack_from('>9H', sub, o)
                        if it[1] < 9: break
                        if it[3:6] == (6, 0x0bc0, COUNTRY): seen[it[6:9]] += 1
                        o += 2 * it[1]
                    if seen: out[(L['level'], D['y'], x, row, (r['k'] - 1) // 2)] = seen.most_common(1)[0][0]
    return out


def area_of(level, pc, reach=8):
    """A parcel's area codes: its own, else those of most parcels on the nearest ring within
    reach that has any, else zeros, which the factory writes too."""
    per = 32 // DIRM[level]   # parcels to a mesh
    if area_of.grid is None:
        area_of.grid = {(lv, y * (32 // DIRM[lv]) + row, x * (32 // DIRM[lv]) + col): a
                        for (lv, y, x, row, col), a in AREA_BASE.items()}
    y, x, row, col = pc
    R, C = y * per + row, x * per + col
    for d in range(reach + 1):
        ring = collections.Counter(area_of.grid[(level, R + dr, C + dc)]
                                   for dr in range(-d, d + 1) for dc in range(-d, d + 1)
                                   if max(abs(dr), abs(dc)) == d and (level, R + dr, C + dc) in area_of.grid)
        if ring: return ring.most_common(1)[0][0]
    return (0, 0, 0)
area_of.grid = None


def replace_names(plain, section):
    """A background record with its names section swapped for section and every other section
    kept byte for byte, in its place. The new section already starts with the record's own
    place, water and POI names (--keep-names)."""
    w = list(struct.unpack_from('>22H', plain))
    secs = sorted((w[i], w[i + 1], i) for i in (7, 9, 11, 13, 15, 17) if w[i] != 0xffff)
    p = 11
    for off, ln, i in secs:
        assert off == p, 'sections are not back to back: %s' % w[7:19]
        p += ln
    assert 4 * p == len(plain), 'record does not end with its last section'
    if w[11] == 0xffff:
        secs.append((p, 0, 11))
    out = b''; p = 11
    for off, ln, i in secs:
        blk = section if i == 11 else plain[4 * off:4 * (off + ln)]
        assert len(blk) % 4 == 0
        w[i], w[i + 1] = p, len(blk) // 4
        out += blk; p += len(blk) // 4
    return struct.pack('>22H', *w) + out


def place_shapes(recs, bydir):
    """Road-shape records into the even pairs of the background directories, names merged in."""
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
        bgrec = s['recs'][bi]
        old = FMT.inflate(bgrec['raw'])
        newpl = replace_names(old, section) if KEEP_BASE is not None else merge_names(old, section)
        newc = FMT.deflate(newpl)
        bgrec['raw'] = newc; bgrec['words'] = len(newpl) // 2
        s['recs'].insert(bi, dict(k=2 * col, raw=fcomp, words=len(fplain) // 2))
        body2 = bytearray(s['body']); struct.pack_into('>II', body2, 8 * (2 * col), len(fcomp) // 2, len(fplain) // 2)
        struct.pack_into('>II', body2, 8 * (2 * col + 1), len(newc) // 2, len(newpl) // 2); s['body'] = bytes(body2); placed += 1
    return placed

if __name__ == '__main__':
    main()
