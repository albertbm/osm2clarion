#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""HOUSE001 building files: the level-0 map the unit draws at building scale.

Read off the stock US (.161) and EUR (.120) cards. A HOUSE001 file covers one level-2
parcel, 7.5' x 5' (an eighth of a mesh each way), named B<mesh y><row><1024 - mesh x><col>
in hex, so Everett MA is B3F40AC7.DAT. It is a map tile with one section, id 5, and one
level, 0 (type 0x91). The section table sits at 0x1000 (slot 14 of the 0x800 list points
there), with one directory of 16 x 16 parcels, kind 0x0826.

A parcel carries up to two records. The even one (w2 = 0x000f) holds the drawing in a
12-pair layer table with 8-byte units; footprints are layer 6, class 0x064 (0x4064 with the
level-0 prefix). Shapes carry two more words than level 1: the first is a floor count
(1..13 in the US file), the second 0. The odd record holds names and is optional, so it is
left out here. MMAIN001.DAT counts the files in its section 5 row.

    house_encode.py LANDUSE.json OUTDIR [--kind building] [--floors N] [--template TILE.DAT]
                    [--bbox lon0,lat0,lon1,lat1]

LANDUSE.json is extract_landuse.py --buildings output. The template supplies the data
version, database box and release string (any of the card's MAPAL001 tiles). --bbox writes
exactly the files covering the box, empty ones included, so a build knows its outputs ahead.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tile_header
from clarion_tile import enc_addr, pad256

UNITS = 2048
GRID = 16
LAYER, CLASS, KIND = 6, 0x4064, 0x80
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


def group_bytes(shapes, floors):
    subs = [shapes[i:i + 255] for i in range(0, len(shapes), 255)]
    bodies = [b''.join(shape_bytes(s, floors) for s in sub) for sub in subs]
    hw = 2 + 2 * len(subs) - 1
    out = bytearray(struct.pack('>HH', len(subs), hw))
    pos = 2 * hw
    for i, (sub, body) in enumerate(zip(subs, bodies)):
        out += bytes([KIND, len(sub)])
        pos += len(body)
        if i < len(subs) - 1:
            out += struct.pack('>H', pos // 2)
    for body in bodies:
        out += body
    while len(out) % 8:
        out.append(0)
    return bytes(out)


def record(w0, w1, shapes, floors):
    g = group_bytes(shapes, floors)
    table = [(0xffff, 0)] * (TABLE_PAIRS - 1) + [(0, 0)]
    table[LAYER] = (TABLE_PAIRS // 2, len(g) // 8)
    geo = b''.join(struct.pack('>HH', *p) for p in table) + g
    w = [w0, w1, 0x000f, 0x0d1c, 0x0ab9, 0, 0, HDR_WORDS // 4, len(geo) // 8] + [0xffff, 0] * 5 + [0] * 5
    return struct.pack('>%dH' % HDR_WORDS, *w) + geo


def compress(plain):
    import bg_encode
    c = zlib.compressobj(6, zlib.DEFLATED, bg_encode.ZWBITS, 8)
    comp = c.compress(plain) + c.flush()
    return comp + b'\0' * (len(comp) % 2)


def parcel_shapes(polys, tree, lon0, lat0, w, h):
    import bg_encode
    from shapely.geometry import box
    rect = box(lon0, lat0, lon0 + w, lat0 + h)
    out = []
    for i in tree.query(rect):
        p = polys[i]
        if not p.intersects(rect):
            continue
        inter = p.intersection(rect)
        for q in (inter.geoms if hasattr(inter, 'geoms') else [inter]):
            if q.geom_type == 'Polygon' and not q.is_empty:
                s = bg_encode.ring_shape(q.exterior, lon0, lat0, w, h, CLASS)
                if s:
                    out.append(s)
    return out


def build_file(cell, polys, tree, hdr, floors=1, keep_empty=False):
    """Bytes of one HOUSE001 file, or None when no footprint lands in it (unless keep_empty)."""
    my, r, mx, c = cell
    lon0, lat0, cw, ch = cell_geo(*cell)
    pw, ph = cw / GRID, ch / GRID
    rows = {}
    for s in range(GRID):
        for k in range(GRID):
            shapes = parcel_shapes(polys, tree, lon0 + k * pw, lat0 + s * ph, pw, ph)
            if shapes:
                plain = record(*code(my, r, s, mx, c, k), shapes, floors)
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


def load(path, kind):
    import json
    from shapely.geometry import shape
    polys = []
    for f in json.load(open(path))['features']:
        if f['properties'].get('kind') != kind:
            continue
        g = shape(f['geometry'])
        polys += [p for p in (g.geoms if hasattr(g, 'geoms') else [g]) if p.geom_type == 'Polygon']
    return polys


def main(argv):
    def opt(name, default):
        if name in argv:
            i = argv.index(name); v = argv[i + 1]; del argv[i:i + 2]
            return v
        return default
    kind = opt('--kind', 'building')
    floors = int(opt('--floors', '1'))
    template = opt('--template', None)
    bbox = opt('--bbox', None)
    src, outdir = argv
    hdr = dict(db_bounds=(4800, 1280, -1200, 4080), data_version='17/09/01/01', source=None)
    if template:
        hdr = tile_header.fields(open(template, 'rb').read(0x1000))[0]
    from shapely.strtree import STRtree
    polys = load(src, kind)
    tree = STRtree(polys)
    cells = set(box_cells(*map(float, bbox.split(',')))) if bbox else set()
    for p in ([] if bbox else polys):
        a, b, c_, e = p.bounds
        for lon in (a, c_):
            for lat in (b, e):
                cells.add(cell_of(lon, lat))
    os.makedirs(outdir, exist_ok=True)
    for cell in sorted(cells):
        data = build_file(cell, polys, tree, hdr, floors, keep_empty=bool(bbox))
        if data:
            name = file_name(*cell)
            open(os.path.join(outdir, name), 'wb').write(data)
            print('wrote', name, len(data))


if __name__ == '__main__':
    main(sys.argv[1:])
