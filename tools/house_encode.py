#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""HOUSE001 building files: the level-0 map the unit draws at building scale, one file per
level-2 parcel (7.5' x 5'), named B<mesh y><row><1024-mesh x><col>, section 5 level 0.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tile_header
from clarion_tile import enc_addr, pad256

UNITS = 2048
GRID = 16
AREA = 0x80
# OSM kind -> (layer, class), read off the stock US and EUR files by locating known places
KIND_CLASSES = {'building': (6, 0x4064), 'forest': (1, 0x408c), 'park': (1, 0x40d9),
                'heath': (1, 0x40d9), 'water': (2, 0x4060), 'river': (2, 0x4060)}
# EUR has no street-area class; 0x40c7 (US port land) stands in, drawn grey
ROAD_LAYER, ROAD_CLASS = 4, 0x40c7
WIDTH = {'motorway': 14, 'trunk': 12, 'primary': 11, 'secondary': 10, 'tertiary': 8,
         'unclassified': 6, 'residential': 6, 'living_street': 5, 'service': 4}
HDR_WORDS, TABLE_PAIRS = 24, 12
DIR_OFF, TABLE_OFF = 0x1100, 0x1000


def file_name(my, r, mx, c):
    return 'B%02X%X%03X%X.DAT' % (my, r, 1024 - mx, c)


def cell_of(lon, lat):
    """(mesh y, row, mesh x, col) of the HOUSE001 file holding a point."""
    y, x = lat * 1.5, lon + 924
    my, mx = int(y), int(x)
    return my, int((y - my) * 8), mx, int((x - mx) * 8)


def box_cells(lon0, lat0, lon1, lat1):
    """Every file touching a box, south to north and west to east."""
    (ya, ra, xa, ca), (yb, rb, xb, cb) = cell_of(lon0, lat0), cell_of(lon1 - 1e-9, lat1 - 1e-9)
    return [((i >> 3), i & 7, (j >> 3), j & 7)
            for i in range(ya * 8 + ra, yb * 8 + rb + 1) for j in range(xa * 8 + ca, xb * 8 + cb + 1)]


def cell_geo(my, r, mx, c):
    """South-west corner and size in degrees of one file."""
    return mx - 924 + c / 8, (my + r / 8) / 1.5, 1 / 8, 1 / 12


def code(my, r, s, mx, c, k):
    """w0, w1 of the parcel at row s, column k (0..15) of a file."""
    R, C = my >> 1, mx >> 1
    w0 = (((C >> 8) << 7 | R) << 8) | (my & 1) << 7 | r << 4 | (s & 3) << 2 | s >> 2
    w1 = (C & 0xff) << 8 | (mx & 1) << 7 | c << 4 | (k & 3) << 2 | k >> 2
    return w0, w1


def shape_bytes(s, floors):
    pts = s['pts']
    out = bytearray(struct.pack('>7H', len(pts), s['type'], floors, 0, 0xffff, *pts[0]))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        out += struct.pack('>bb', x1 - x0, y1 - y0)
    return out


def group_bytes(parts):
    """One layer's group from (kind, floors, shapes) parts, at most 255 shapes per sub-group."""
    subs = [(kind, floors, shapes[i:i + 255]) for kind, floors, shapes in parts
            for i in range(0, len(shapes), 255)]
    bodies = [b''.join(shape_bytes(s, floors) for s in sh) for _, floors, sh in subs]
    hw = 2 + 2 * len(subs) - 1
    out = bytearray(struct.pack('>HH', len(subs), hw))
    pos = 2 * hw
    for i, ((kind, _, sh), body) in enumerate(zip(subs, bodies)):
        out += bytes([kind, len(sh)])
        pos += len(body)
        if i < len(subs) - 1:
            out += struct.pack('>H', pos // 2)
    for body in bodies:
        out += body
    while len(out) % 8:
        out.append(0)
    return bytes(out)


def record(w0, w1, layers):
    """The even record of a parcel; layers maps a layer number to its group parts."""
    table = [(0xffff, 0)] * (TABLE_PAIRS - 1) + [(0, 0)]
    pos, groups = TABLE_PAIRS // 2, b''
    for layer in sorted(layers):
        g = group_bytes(layers[layer])
        table[layer] = (pos, len(g) // 8)
        pos += len(g) // 8
        groups += g
    geo = b''.join(struct.pack('>HH', *p) for p in table) + groups
    w = [w0, w1, 0x000f, 0x0d1c, 0x0ab9, 0, 0, HDR_WORDS // 4, len(geo) // 8] + [0xffff, 0] * 5 + [0] * 5
    return struct.pack('>%dH' % HDR_WORDS, *w) + geo


def compress(plain):
    import bg_encode
    c = zlib.compressobj(6, zlib.DEFLATED, bg_encode.ZWBITS, 8)
    comp = c.compress(plain) + c.flush()
    return comp + b'\0' * (len(comp) % 2)


def parts(g):
    return list(getattr(g, 'geoms', [g]))


def no_holes(p):
    """Hole-free pieces of a polygon, cut through each hole; the unit fills outer rings only."""
    from shapely.geometry import box
    if not p.interiors:
        return [p]
    x = p.interiors[0].centroid.x
    a, b, c, d = p.bounds
    out = []
    for half in (box(a - 1, b - 1, x, d + 1), box(x, b - 1, c + 1, d + 1)):
        out += [q for piece in parts(p.intersection(half)) if piece.geom_type == 'Polygon'
                for q in no_holes(piece)]
    return out


class Layer:
    """One polygon class of one layer, indexed for clipping."""
    def __init__(self, layer, cls, kind, geoms, floors=0):
        from shapely.strtree import STRtree
        self.layer, self.cls, self.kind, self.floors = layer, cls, kind, floors
        self.geoms = [g for g in geoms if not g.is_empty]
        self.tree = STRtree(self.geoms)

    def shapes(self, rect, lon0, lat0, w, h):
        import bg_encode
        out = []
        for i in self.tree.query(rect):
            g = self.geoms[i]
            if not g.intersects(rect):
                continue
            inter = g.intersection(rect)
            for q in parts(inter):
                if q.geom_type != 'Polygon' or q.is_empty:
                    continue
                for piece in no_holes(q):
                    s = bg_encode.ring_shape(piece.exterior, lon0, lat0, w, h, self.cls)
                    if s:
                        out.append(s)
        return out


def build_file(cell, layers, hdr, keep_empty=False, counts=None, verts=None, peak=None, hard=None, dense=0):
    """Bytes of one HOUSE001 file, or None when nothing lands in it (unless keep_empty)."""
    from shapely.geometry import box
    my, r, mx, c = cell
    lon0, lat0, cw, ch = cell_geo(*cell)
    pw, ph = cw / GRID, ch / GRID
    rows = {}
    for s in range(GRID):
        for k in range(GRID):
            x0, y0 = lon0 + k * pw, lat0 + s * ph
            rect = box(x0, y0, x0 + pw, y0 + ph)
            groups = {}
            for L in layers:
                shapes = L.shapes(rect, x0, y0, pw, ph)
                if hard and L is layers[-1] and sum(len(x['pts']) for x in shapes) > dense:
                    shapes = hard.shapes(rect, x0, y0, pw, ph)
                    if counts is not None:
                        counts[('hard', 0)] = counts.get(('hard', 0), 0) + 1
                if shapes:
                    groups.setdefault(L.layer, []).append((L.kind, L.floors, shapes))
                    if counts is not None:
                        counts[(L.layer, L.cls)] = counts.get((L.layer, L.cls), 0) + len(shapes)
                    if verts is not None:
                        verts[(L.layer, L.cls)] = verts.get((L.layer, L.cls), 0) + sum(len(x['pts']) for x in shapes)
            if groups and peak is not None:
                n = [sum(len(x['pts']) for _, _, sh in g for x in sh) for g in groups.values()]
                m = [sum(len(sh) for _, _, sh in g) for g in groups.values()]
                peak[:] = max(peak or [(0, 0, 0, 0)], [(sum(n), sum(m), s, k)])[:1]
            if groups:
                plain = record(*code(my, r, s, mx, c, k), groups)
                rows.setdefault(s, {})[k] = (plain, compress(plain))
    if not rows and not keep_empty:
        return None

    y0, x0 = r << 10 | my, c << 10 | mx
    y1, x1 = ((r + 1) & 7) << 10 | (my + 1), ((c + 1) & 7) << 10 | (mx + 1)
    d = bytearray(tile_header.author(
        [(5, [(0, 0x91, 1)])],
        [(5, [(0, 0, (my * 40 + r * 5 + 5, my * 40 + r * 5,
                      int(lon0 * 60), int((lon0 + cw) * 60)))])],
        hdr['db_bounds'], hdr['data_version'], hdr['source']))
    d[0x4ed:0x4f4] = b'\0' + b'\xff' * 6
    struct.pack_into('>IH', d, 0x800 + 6 * 14, enc_addr(TABLE_OFF), 1)
    d += b'\0' * (DIR_OFF - len(d))
    struct.pack_into('>10H', d, TABLE_OFF, 10, 2, 8, 1, y1, y0, x0, x1, 0, enc_addr(DIR_OFF))

    dirb = bytearray(struct.pack('>6H', 0x0826, GRID, GRID, y0, x0, 0x91))
    blocks = bytearray()
    pos = pad256(DIR_OFF + 12 + GRID * 260)
    for s in range(GRID):
        if s not in rows:
            dirb += b'\xff' * 260
            continue
        pairs = [b'\xff' * 8] * 32
        blk = bytearray()
        for k, (plain, comp) in sorted(rows[s].items()):
            pairs[2 * k] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
            blk += comp
        dirb += struct.pack('>I', enc_addr(pos)) + b''.join(pairs)
        blk += b'\0' * (pad256(len(blk)) - len(blk))
        blocks += blk
        pos += len(blk)
    d[DIR_OFF:DIR_OFF + len(dirb)] = dirb
    d += b'\0' * (pad256(len(d)) - len(d))
    d += blocks
    return bytes(d)


def metric(lat):
    """Degrees to local metres and back, for merging and buffering."""
    import math, shapely
    kx, ky = 111320 * math.cos(math.radians(lat)), 110540
    to_m = lambda g: shapely.transform(g, lambda a: a * (kx, ky))
    to_deg = lambda g: shapely.transform(g, lambda a: a / (kx, ky))
    return to_m, to_deg


def polygons(g):
    return [p for p in parts(g) if p.geom_type == 'Polygon' and not p.is_empty]


def load(path):
    """Polygons of a LANDUSE.json by kind."""
    import json
    from shapely.geometry import shape
    by = {}
    for f in json.load(open(path))['features']:
        by.setdefault(f['properties'].get('kind'), []).extend(polygons(shape(f['geometry'])))
    return by


def merge_blocks(polys, to_m, to_deg, gap=0.5, tol=2.0, min_area=30):
    """Touching footprints merged into blocks, simplified, tiny ones dropped."""
    from shapely.ops import unary_union
    u = unary_union([to_m(p).buffer(gap / 2) for p in polys]).buffer(-gap / 2)
    out = [p.simplify(tol, preserve_topology=True) for p in polygons(u)]
    return [to_deg(p) for p in out if p.area >= min_area]


def areas(polys, to_m, to_deg, tol=3.0, min_area=100, within=None):
    from shapely.ops import unary_union
    u = unary_union([to_m(p).buffer(0) for p in polys])
    if within is not None:
        u = u.intersection(to_m(within))
    return [to_deg(p) for p in polygons(u.simplify(tol, preserve_topology=True)) if p.area >= min_area]


def road_layers(path, to_m, to_deg):
    """OSM roads buffered by a width per class and merged into one street-area layer."""
    import json
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    polys = [to_m(LineString(w['nodes'])).buffer(WIDTH[w['hw']] / 2, cap_style='flat')
             for w in json.load(open(path)) if w.get('hw') in WIDTH and len(w['nodes']) >= 2]
    u = unary_union(polys).simplify(0.5)
    return [Layer(ROAD_LAYER, ROAD_CLASS, AREA, [to_deg(p) for p in polygons(u)])]


def coast_land(path, bbox):
    """Land inside the box from coastline ways (bg_tiles.load_land), and the sea around it."""
    from shapely.geometry import box
    import bg_tiles
    land = bg_tiles.load_land(path)[0].intersection(box(*bbox).buffer(0.01))
    return land, box(*bbox).difference(land)


def main(argv):
    def opt(name, default):
        if name in argv:
            i = argv.index(name); v = argv[i + 1]; del argv[i:i + 2]
            return v
        return default
    kinds = opt('--kinds', ','.join(KIND_CLASSES)).split(',')
    floors = int(opt('--floors', '1'))
    template = opt('--template', None)
    bbox = opt('--bbox', None)
    roads = opt('--roads', None)
    coast = opt('--coast', None)
    dense = int(opt('--dense', '0'))
    stats = opt('--stats', None)
    src, outdir = argv
    hdr = dict(db_bounds=(4800, 1280, -1200, 4080), data_version='17/09/01/01', source=None)
    if template:
        hdr = tile_header.fields(open(template, 'rb').read(0x1000))[0]
    by = load(src)
    allp = [p for k in kinds for p in by.get(k, [])]
    if not allp:
        raise SystemExit('no polygons of kinds %s in %s' % (','.join(kinds), src))
    to_m, to_deg = metric(sum(p.centroid.y for p in allp) / len(allp))
    land = sea = None
    if coast:
        assert bbox, '--coast needs --bbox'
        land, sea = coast_land(coast, tuple(map(float, bbox.split(','))))
    layers = []
    for cls in sorted({KIND_CLASSES[k] for k in kinds if k != 'building'}):
        polys = [p for k in kinds if KIND_CLASSES[k] == cls for p in by.get(k, [])]
        water = cls == KIND_CLASSES['water']
        if water and sea is not None:
            polys += polygons(sea)
        if polys:
            layers.append(Layer(*cls, AREA, areas(polys, to_m, to_deg, within=None if water else land)))
    if roads:
        layers += road_layers(roads, to_m, to_deg)
    hard = None
    BLD = KIND_CLASSES['building']
    if 'building' in kinds and by.get('building'):
        merged = merge_blocks(by['building'], to_m, to_deg)
        print('buildings: %d footprints in, %d blocks' % (len(by['building']), len(merged)))
        layers.append(Layer(*BLD, AREA, merged, floors))
        if dense:
            hard = Layer(*BLD, AREA, merge_blocks(by['building'], to_m, to_deg, gap=1.0, tol=4.0, min_area=60), floors)
    cells = set(box_cells(*map(float, bbox.split(',')))) if bbox else set()
    for p in ([] if bbox else allp):
        a, b, c_, e = p.bounds
        for lon in (a, c_):
            for lat in (b, e):
                cells.add(cell_of(lon, lat))
    os.makedirs(outdir, exist_ok=True)
    from shapely.geometry import box
    from shapely.strtree import STRtree
    fp = [p.representative_point() for p in by.get('building', [])] if 'building' in kinds else []
    fptree = STRtree(fp) if fp else None
    counts, table = {}, []
    for cell in sorted(cells):
        cc, cv, pk = {}, {}, []
        data = build_file(cell, layers, hdr, keep_empty=bool(bbox), counts=cc, verts=cv, peak=pk,
                          hard=hard if layers[-1].cls == BLD[1] else None, dense=dense)
        hardened = cc.pop(('hard', 0), 0)
        for k, n in cc.items():
            counts[k] = counts.get(k, 0) + n
        if data:
            name = file_name(*cell)
            open(os.path.join(outdir, name), 'wb').write(data)
            lon0, lat0, cw, ch = cell_geo(*cell)
            nin = len(fptree.query(box(lon0, lat0, lon0 + cw, lat0 + ch), predicate='contains')) if fptree else 0
            table.append(dict(file=name, lon=round(lon0, 4), lat=round(lat0, 4), footprints=nin,
                              buildings=cc.get(BLD, 0), building_verts=cv.get(BLD, 0),
                              shapes=sum(cc.values()), verts=sum(cv.values()), bytes=len(data), hardened=hardened,
                              peak_parcel=dict(zip(('verts', 'shapes', 'row', 'col'), pk[0])) if pk else None))
            print('wrote %s %d bytes: %d footprints in, %d shapes, %d vertices%s'
                  % (name, len(data), nin, sum(cc.values()), sum(cv.values()),
                     ' (%d dense parcels)' % hardened if hardened else ''))
    print('shapes out:', ', '.join('L%d/%04x %d' % (l, c, n) for (l, c), n in sorted(counts.items())))
    if stats:
        import json
        json.dump(table, open(stats, 'w'), indent=1)


if __name__ == '__main__':
    main(sys.argv[1:])
