"""Encode land polygons and coastline ways into Clarion background records and a tile.

Parcel grids (all within a tile cell of 8x8 first-level meshes; a mesh is 1 deg x 40'):
  level 2: directory per 4x4-mesh quadrant, parcel = 7.5' x 5'   (32 cols x 32 rows)
  level 1: directory per mesh,              parcel = 1.875' x 1.25' (32 cols x 32 rows)
Directory slot = parcel row, slot pair index = 2*col + 1 for the background record.
Coordinates inside a parcel are 0..2047 from the south-west corner.
"""
import math, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shapely.geometry import box, LineString, MultiLineString, Polygon, MultiPolygon, GeometryCollection
from shapely.ops import unary_union
import clarion_bg, clarion_tile

from clarion_bg import MESH_W, MESH_H, UNITS, LEVELS, tile_cell, mesh_to_lon, mesh_to_lat, parcel_geo
ZWBITS = 13   # 8 KiB window, as in the shipped files

def quant(v, v0, span):
    return max(0, min(UNITS - 1, int(round((v - v0) / span * UNITS))))

def densify(pts):
    """Insert points so every step fits an int8 delta."""
    out = [pts[0]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, math.ceil(max(abs(x1 - x0), abs(y1 - y0)) / 127))
        for i in range(1, n + 1):
            p = (x0 + round((x1 - x0) * i / n), y0 + round((y1 - y0) * i / n))
            if p != out[-1]: out.append(p)
    return out

MAX_VERTS = 600   # shipped sea polygons never exceed 677; the unit stops filling long ones

def to_parcel(coords, lon0, lat0, w, h, closed=False):
    """Quantise to parcel units, simplifying until the shape fits the vertex budget."""
    from shapely.geometry import LineString, LinearRing
    units = [((lon - lon0) / w * UNITS, (lat - lat0) / h * UNITS) for lon, lat in coords]
    tol = 0.7
    while True:
        g = LinearRing(units) if closed and len(units) >= 4 else LineString(units)
        simp = list(g.simplify(tol, preserve_topology=True).coords)
        pts = [(max(0, min(UNITS - 1, round(x))), max(0, min(UNITS - 1, round(y)))) for x, y in simp]
        dedup = [pts[0]] + [p for a, p in zip(pts, pts[1:]) if p != a]
        out = densify(dedup)
        if len(out) <= MAX_VERTS or tol > 64: return out
        tol *= 2

def ring_shape(ring, lon0, lat0, w, h, typ):
    coords = list(ring.coords)
    if ring.is_ccw: coords.reverse()      # shipped sea polygons run clockwise
    pts = to_parcel(coords, lon0, lat0, w, h, closed=True)
    if pts[0] != pts[-1]: pts.append(pts[0])
    return dict(type=typ, ref=0xffff, pts=pts) if len(pts) >= 4 else None

def line_shapes(geom, lon0, lat0, w, h, typ):
    lines = []
    if geom.is_empty: return lines
    parts = geom.geoms if hasattr(geom, 'geoms') else [geom]
    for g in parts:
        if isinstance(g, LineString) and len(g.coords) >= 2:
            pts = to_parcel(list(g.coords), lon0, lat0, w, h)
            if len(pts) >= 2: lines.append(dict(type=typ, ref=0xffff, pts=pts))
    return lines

def parse_labels(spec):
    """'TYPE:FLAGS:lon,lat:TEXT[:ID];...' -> [(type, flags, lon, lat, text, id)]"""
    out = []
    for item in spec.split(';'):
        if not item: continue
        parts = item.split(':')
        typ, flags = int(parts[0], 0), int(parts[1], 0); lon, lat = (float(v) for v in parts[2].split(','))
        text = parts[3]; idw = int(parts[4], 0) if len(parts) > 4 else 0x35d8
        out.append((typ, flags, lon, lat, text, idw))
    return out

def labels_in(labels, lon0, lat0, w, h):
    """Labels inside a parcel, as name_section entries with parcel coordinates."""
    out = []
    for typ, flags, lon, lat, text, idw in labels:
        if lon0 <= lon < lon0 + w and lat0 <= lat < lat0 + h:
            out.append((typ, flags, quant(lon, lon0, w), quant(lat, lat0, h), text, idw))
    return out

def name_section(labels):
    """labels: list of (type, flags, x, y, text). Header is six (offset, count) pairs
    in 4-byte units from the section start; only the first pair is used here."""
    body = bytearray()
    for lab in labels:
        typ, flags, x, y, text = lab[:5]
        raw = text.encode('utf-8')   # shipped names are UTF-8 (VIA NICÀ)
        if len(raw) % 2: raw += b'\0'
        if flags & 0x4000:   # point labels carry an id word before the length
            body += struct.pack('>HHHHHH', typ, flags, x, y, lab[5] if len(lab) > 5 else 0x35d8, len(raw)) + raw
        else:
            body += struct.pack('>HHHHH', typ, flags, x, y, len(raw)) + raw
    hdr = struct.pack('>HH', 6, len(labels)) + b'\xff\xff\x00\x00' * 5
    out = hdr + body
    while len(out) % 4: out += b'\0'
    return bytes(out)

def build_parcel_record(level, y0, x0, row, col, sea_polys, coast, labels, nlayers=26, area_polys=()):
    """(y0, x0) is the directory's south-west mesh, (row, col) the parcel inside it."""
    L = LEVELS[level]
    if level in (4, 5):   # code units are 20'/30' (2 per mesh); parcels are 2 (level 4) or 8 (level 5) meshes
        m = {4: 2, 5: 8}[level]
        w = [0] * 22
        w[0], w[1] = clarion_bg.parcel_code(level, 2 * (y0 + m * row), 2 * (x0 + m * col))
    else:
        per_row, per_col = L['rows'] // L['dir_meshes'], L['cols'] // L['dir_meshes']
        w = [0] * 22
        w[0], w[1] = clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)
    # level 5 ships 0xfa on records with shapes and 0xfa0 on empty ones; level 4 is 0x3e8 throughout
    w[2] = {4: 0x3e8, 5: 0xfa if (sea_polys or coast or area_polys) else 0xfa0}.get(level, 0x19); w[3], w[4] = L['w34']
    w[7], w[8] = 0xffff, 0
    w[13], w[14] = 0xffff, 0; w[15], w[16] = 0xffff, 0; w[17], w[18] = 0xffff, 0
    w[19], w[20] = 0, 0xffff
    layers = {}
    if area_polys: layers[0] = dict(kind=0x80, shapes=area_polys)
    if sea_polys: layers[1] = dict(kind=0x80, shapes=sea_polys)
    if coast: layers[11] = dict(kind=0x40, shapes=coast)
    sections = {'names': name_section(labels)} if labels else {}   # shipped records omit it when empty
    rec = dict(hdr=w, nlayers=nlayers if layers else 0, layers=layers, sections=sections)
    plain = clarion_bg.build_record(rec)
    c = zlib.compressobj(6, zlib.DEFLATED, ZWBITS, 8)
    comp = c.compress(plain) + c.flush()
    if len(comp) % 2: comp += b'\0'      # the shipped files pad odd streams with a zero byte
    return plain, comp

def encode_level(level, y0, x0, land, coast_lines, coverage, sea_name):
    """Records for one directory. Returns {row: {col: (plain, comp)}}."""
    L = LEVELS[level]
    out = {}
    for row in range(L['rows']):
        for col in range(L['cols']):
            lon0, lat0, w, h = parcel_geo(level, y0, x0, row, col)
            rect = box(lon0, lat0, lon0 + w, lat0 + h)
            if rect.intersection(coverage).area <= 0: continue
            sea = rect.difference(land)
            sea_polys = []
            polys = sea.geoms if hasattr(sea, 'geoms') else [sea]
            for p in polys:
                if not isinstance(p, Polygon) or p.is_empty: continue
                s = ring_shape(p.exterior, lon0, lat0, w, h, L['seatype'])
                if s: sea_polys.append(s)
                # holes (islets) are not representable here; they are dropped
            coast = []
            for way in coast_lines:
                if way.intersects(rect):
                    coast += line_shapes(way.intersection(rect), lon0, lat0, w, h, L['coasttype'])
            labels = []
            if sea_polys and sea_name:
                # put the label on the largest sea polygon's representative point
                big = max((p for p in polys if isinstance(p, Polygon)), key=lambda p: p.area)
                rp = big.representative_point()
                labels.append((0x4089, 0x2800, quant(rp.x, lon0, w), quant(rp.y, lat0, h), sea_name))
            if not sea_polys and not coast: continue   # nothing to draw: no record
            out.setdefault(row, {})[col] = build_parcel_record(level, y0, x0, row, col, sea_polys, coast, labels)
    return out

def make_dir(level, y0, x0, recs):
    """A directory object for clarion_tile.write_tile."""
    L = LEVELS[level]
    body, nslots = L['body'], L['slots']
    npairs = body // 8
    def empty_pair(k):
        return b'\x7f\xff\xff\xff\x7f\xff\xff\xff' if (level == 1 and k % 2 == 0) else b'\xff' * 8
    slots = {}
    for row, cols in sorted(recs.items()):
        pairs = [empty_pair(k) for k in range(npairs)]
        rlist = []
        for col, (plain, comp) in sorted(cols.items()):
            assert len(comp) % 2 == 0 and len(plain) % 2 == 0
            pairs[2 * col + 1] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
            rlist.append(dict(k=2 * col + 1, raw=comp, words=len(plain) // 2))
        slots[row] = dict(body=b''.join(pairs), recs=rlist)
    empty = b''.join(empty_pair(k) for k in range(npairs))
    n = L['dir_meshes']
    return dict(kind=L['kind'], ab=L['ab'], y=y0, x=x0, typ=L['typ'], body=body, nslots=nslots, slots=slots,
                empty_bodies={i: empty for i in range(nslots) if i not in slots},
                box=(y0 + n, y0, x0, x0 + n))

def data_box_arcmin(level, dirs):
    lat0 = lat1 = lon0 = lon1 = None
    for D in dirs:
        for row, cols in D['_recs'].items():
            for col in cols:
                a, b, w, h = parcel_geo(level, D['y'], D['x'], row, col)
                lon0 = a if lon0 is None else min(lon0, a); lon1 = a + w if lon1 is None else max(lon1, a + w)
                lat0 = b if lat0 is None else min(lat0, b); lat1 = b + h if lat1 is None else max(lat1, b + h)
    return (int(math.ceil(lat1 * 60)), int(math.floor(lat0 * 60)), int(math.floor(lon0 * 60)), int(math.ceil(lon1 * 60)))

def build_tile(name, template, land, coast_lines, coverage, sea_name, db_bounds=None):
    """Return tile bytes. template: bytes of a real tile whose 0x1000 header is reused."""
    yb, xb = tile_cell(name)
    dirs = {2: [], 1: []}
    for level in (2, 1):
        n = LEVELS[level]['dir_meshes']
        for y0 in range(yb, yb + 8, n):
            for x0 in range(xb, xb + 8, n):
                area = box(mesh_to_lon(x0), mesh_to_lat(y0), mesh_to_lon(x0 + n), mesh_to_lat(y0 + n))
                if area.intersection(coverage).area <= 0: continue
                recs = encode_level(level, y0, x0, land, coast_lines, coverage, sea_name)
                if not recs: continue
                D = make_dir(level, y0, x0, recs); D['_recs'] = recs
                dirs[level].append(D)
    return assemble_tile(name, template, dirs, db_bounds)

def assemble_tile(name, template, dirs, db_bounds=None):
    """Header tables, section tables and container for directories built by make_dir
    (dirs = {2: [...], 1: [...]}, each dir carrying its '_recs')."""
    header = bytearray(template[:0x1000])
    # level table: section 1 counts, section 2 empty
    lv = struct.pack('>H', 2) + bytes([1, 6]) + b''.join(struct.pack('>BBH', l, t, c) for l, t, c in
         [(6, 0x80, 0), (5, 0x90, 0), (4, 0x91, 0), (3, 0x91, 0), (2, 0x91, len(dirs[2])), (1, 0x92, len(dirs[1]))]) \
         + bytes([2, 4]) + b''.join(struct.pack('>BBH', l, t, 0) for l, t in [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)])
    header[0x1ac:0x1ac + len(lv)] = lv
    bb = struct.pack('>H', 2) + bytes([1, 2]) + b''.join(struct.pack('>BB4h', l, 0, *data_box_arcmin(l, dirs[l])) for l in (2, 1)) + bytes([2, 0])
    header[0x2ac:0x2ac + 0x30] = b'\xff' * 0x30
    header[0x2ac:0x2ac + len(bb)] = bb
    if db_bounds:
        struct.pack_into('>hhhh', header, 0x184, *db_bounds)
        struct.pack_into('>hh', header, 0x5d0, db_bounds[1], db_bounds[2])
    sec1 = dict(id=1, count=0, levels=[dict(level=l, typ=t, dirs=dirs.get(l, [])) for l, t in [(6, 0x80), (5, 0x90), (4, 0x91), (3, 0x91), (2, 0x91), (1, 0x92)]])
    sec2 = dict(id=2, count=0, levels=[dict(level=l, typ=t, dirs=[]) for l, t in [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)]])
    t = dict(header=header, sections=[sec1, sec2])
    for sec in t['sections']:
        nw, _, _ = clarion_tile.table_words(sec); sec['count'] = -(-2 * nw // 256)
    return clarion_tile.write_tile(t)

def load_geojson_lines(path):
    import json
    g = json.load(open(path))
    lines = []
    for f in g['features']:
        geom = f['geometry']
        if geom['type'] == 'LineString': lines.append(LineString(geom['coordinates']))
        elif geom['type'] == 'MultiLineString': lines += [LineString(c) for c in geom['coordinates']]
    return lines

def land_from_coastline(lines):
    """OSM coastline ways form closed rings; polygonize them."""
    from shapely.ops import linemerge, polygonize
    merged = linemerge(MultiLineString(lines))
    polys = list(polygonize(merged))
    if not polys: raise SystemExit('coastline does not form closed rings')
    return unary_union(polys)
