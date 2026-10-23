# SPDX-License-Identifier: GPL-3.0-or-later
"""Background (section 1) record: parse to a dict and build back to bytes.

Record layout (all big-endian, sizes in 16-bit words unless noted; "unit" = 4 bytes):
  w0,w1   parcel code (row/col), see parcel_code()
  w2      record type, 0x0019 = background
  w3,w4   ffff ffff at level 2, 347a 2aef at level 1 (meaning unknown)
  w5,w6   0
  w7..w20 seven (offset, length) pairs in units from record start:
          (w7,w8) unused here, (w9,w10) geometry, (w11,w12) names,
          (w13,w14) unused, (w15,w16) point index, (w17,w18) unused, (w19,w20) = 0000 ffff
  w21     0
  geometry section at unit 11: layer table of N (offset, length) unit pairs relative to
          the section start, ffff/0000 when absent, N = offset of the first group;
          then the groups.
  group   0001 0003, u8 kind (0x80 polygon, 0x40 polyline, 0x00 point), u8 count, shapes,
          zero padded to a unit boundary.
  shape   u16 nvertices, u16 type, u16 ref (ffff), u16 x0, u16 y0, (n-1) x int8 dx,dy.
          Polygons repeat the first vertex as the last one. Points: type, ref, x, y.

Only this form is modelled. A road-shape record (`fa_encode`, `lev_roads.fa_record`) shares
the w2 = 0x00fa kind but counts shapes in a pair's second word rather than measuring units, so
parse_record refuses it instead of returning a record short of its tail.
"""
import struct

MAGIC = b'\x00\x01\x00\x03'

# Parcel grid. Mesh coordinates: y = lat * 1.5, x = lon + 924 (a mesh is 1 deg x 40').
MESH_W, MESH_H = 1.0, 2/3
UNITS = 2048
LEVELS = {5: dict(cols=11, rows=11, dir_meshes=88, typ=0x90, seatype=0x8012, coasttype=0x8007, areatype=0x802b, w34=(0xffff, 0xffff),
                  kind=0x0200, ab=(0xb, 0xb), body=88, slots=11),
          4: dict(cols=44, rows=44, dir_meshes=88, typ=0x91, seatype=0x8012, coasttype=0x8007, areatype=0x802b, w34=(0xffff, 0xffff),
                  kind=0x3cde, ab=(0x2c, 0x2c), body=704, slots=44),
          3: dict(cols=16, rows=16, dir_meshes=8, typ=0x91, seatype=0xa012, coasttype=0xa007, areatype=0xa02b, w34=(0xffff, 0xffff),
                  kind=0x0826, ab=(0x10, 0x10), body=256, slots=16),
          2: dict(cols=32, rows=32, dir_meshes=4, typ=0x91, seatype=0x5012, coasttype=0x5007, areatype=0x502b, w34=(0xffff, 0xffff),
                  kind=0x2046, ab=(0x20, 0x20), body=512, slots=32),
          1: dict(cols=32, rows=32, dir_meshes=1, typ=0x92, seatype=0xc012, coasttype=0xc007, areatype=0xc02b, w34=(0x347a, 0x2aef),
                  kind=0x2046, ab=(0x20, 0x20), body=512, slots=32)}

# Area kinds (extract_landuse.py) to (section-1 layer, shape class). The class is the low 12
# bits of the shape type; the level's prefix (LEVELS seatype & 0xf000) goes on top. Read off
# the factory Koeln tile B48R060R against known places: 01a Siebengebirge and Eifel forest,
# 01d town parks (Mariemont, Guetersloh), 015 Rursee and Laacher See, 014 the Rhine at Mainz.
AREA_CLASSES = {'forest': (3, 0x01a), 'heath': (3, 0x01b), 'park': (3, 0x01d),
                'water': (7, 0x015), 'river': (7, 0x014)}

def tile_cell(name):
    """South-west mesh (y0, x0) of tile BbbRrrr: filenames are hexadecimal."""
    b = int(name[1:3], 16); r = int(name[4:7], 16)
    return b, 1024 - r

def mesh_to_lon(x): return x - 924
def mesh_to_lat(y): return y / 1.5

def parcel_geo(level, y0, x0, row, col):
    """Lower-left corner and size in degrees of a parcel of a directory whose
    south-west mesh is (y0, x0)."""
    L = LEVELS[level]
    w = MESH_W * L['dir_meshes'] / L['cols']; h = MESH_H * L['dir_meshes'] / L['rows']
    return mesh_to_lon(x0) + col * w, mesh_to_lat(y0) + row * h, w, h


def s8(b): return b - 256 if b > 127 else b

def parcel_code(level, row, col):
    """w0, w1 for a parcel at absolute (row, col). Level 3 units: 20' rows, 30' cols
    (2 per mesh). Level 2: 5' x 7.5' (8 per mesh). Level 1: 1.25' x 1.875' (32 per mesh)."""
    if level in (3, 4, 5, 6):   # rows of 20', columns of 30' (2 per mesh); level 4 parcels are 2x2 meshes, level 5 8x8
        R, C = row >> 2, col >> 2
        lo0 = ((row & 2) << 6) | ((row & 1) << 4) | 0xf
        lo1 = ((col & 2) << 6) | ((col & 1) << 4) | {3: 0x3, 4: 0x4, 5: 0x5, 6: 0x6}[level]
    elif level == 2:
        R, C = row >> 4, col >> 4
        lo0 = ((row & 0xf) << 4) | 0xf
        lo1 = ((col & 0xf) << 4) | 0x2
    else:
        R, C = row >> 6, col >> 6
        lo0 = (((row & 0x3f) >> 2) << 4) | 0xc | (row & 3)
        lo1 = (((col & 0x3f) >> 2) << 4) | 0xc | (col & 3)
    return (((C >> 8) << 7 | R) << 8) | lo0, ((C & 0xff) << 8) | lo1

def parse_shapes(d, p, kind, n):
    shapes = []
    for _ in range(n):
        if kind == 0:
            typ, ref, x, y = struct.unpack_from('>HHHH', d, p); p += 8
            shapes.append(dict(type=typ, ref=ref, pts=[(x, y)]))
        else:
            cnt, typ, ref, x, y = struct.unpack_from('>HHHHH', d, p); p += 10
            pts = [(x, y)]
            for _ in range(cnt - 1):
                x += s8(d[p]); y += s8(d[p+1]); p += 2
                pts.append((x, y))
            shapes.append(dict(type=typ, ref=ref, pts=pts))
    return p, shapes

def parse_group(d, p, end):
    """A layer: u16 sub-group count, u16 header words, then per sub-group a kind byte, a
    count byte and (all but the last) the u16 word offset where it ends. One sub-group
    gives the common 0001 0003 header. Extra sub-groups are kept as 'more'."""
    start = p
    nsub, hw = struct.unpack_from('>HH', d, p)
    if nsub == 0 or nsub > 8 or hw != 2 + 2 * nsub - 1:
        return dict(raw=d[p:end])
    q = p + 4; subs = []
    for i in range(nsub):
        kind, n = d[q], d[q+1]; q += 2
        stop = None
        if i < nsub - 1: stop = start + 2 * struct.unpack_from('>H', d, q)[0]; q += 2
        subs.append((kind, n, stop))
    try:
        parsed = []
        for kind, n, stop in subs:
            q, shapes = parse_shapes(d, q, kind, n)
            if stop is not None and q != stop: return dict(raw=d[start:end], note='sub-group length mismatch')
            parsed.append(dict(kind=kind, shapes=shapes))
    except struct.error:
        return dict(raw=d[start:end], note='truncated')
    pad = d[q:end]
    if len(pad) >= 4 or any(pad):
        return dict(raw=d[start:end], note='unparsed tail')
    g = parsed[0]
    if len(parsed) > 1: g['more'] = parsed[1:]
    return g

def build_shapes(kind, shapes):
    out = bytearray()
    for s in shapes:
        pts = s['pts']
        if kind == 0:
            out += struct.pack('>HHHH', s['type'], s['ref'], pts[0][0], pts[0][1])
        else:
            out += struct.pack('>HHHHH', len(pts), s['type'], s['ref'], pts[0][0], pts[0][1])
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                dx, dy = x1 - x0, y1 - y0
                assert -128 <= dx <= 127 and -128 <= dy <= 127, (dx, dy)
                out += struct.pack('>bb', dx, dy)
    return bytes(out)

def build_group(g):
    if 'raw' in g: return g['raw']
    subs = [g] + g.get('more', [])
    bodies = [build_shapes(s['kind'], s['shapes']) for s in subs]
    hw = 2 + 2 * len(subs) - 1
    out = bytearray(struct.pack('>HH', len(subs), hw))
    pos = 2 * hw
    for i, (s, body) in enumerate(zip(subs, bodies)):
        out += bytes([s['kind'], len(s['shapes'])])
        pos += len(body)
        if i < len(subs) - 1: out += struct.pack('>H', pos // 2)
    for body in bodies: out += body
    while len(out) % 4: out.append(0)
    return bytes(out)

def bg_slot(w):
    """Which pointer pair holds a record's geometry.

    A road-only record (the even pair at levels 3 and 4) keeps it at w7/w8 and a background
    record at w9/w10, but a level-5 record has w2 = 0xfa either way and carries both, so the
    slot has to come off the pointers rather than off w2. An unused pair reads ffff/0000 as
    shipped and 0000/0000 on a record rebuilt against the 2014 database; neither can be a real
    offset, since the 44-byte header puts the first one at unit 11. w9/w10 wins when both are
    filled, and stands in when neither is.
    """
    if w[9] not in (0, 0xffff) and w[10]: return 9
    if w[7] != 0xffff and w[8]: return 7
    return 9

def parse_record(d):
    w = list(struct.unpack_from('>22H', d, 0))
    assert w[2] in (0x19, 0x3e8, 0xfa, 0xfa0), 'not a background record (w2=%#x)' % w[2]   # 0xfa: roads (even pair) and level 5
    gi = bg_slot(w)
    geo_off, geo_len = w[gi], w[gi + 1]
    base = 4 * geo_off
    table = []; nlayers = 1 << 30 if geo_off != 0xffff else 0
    while len(table) < nlayers:
        off, ln = struct.unpack_from('>HH', d, base + 4*len(table)); table.append((off, ln))
        if off != 0xffff: nlayers = min(nlayers, off)
    assert nlayers == len(table), (nlayers, len(table))
    if geo_off == 0xffff: assert geo_len == 0
    # A road-shape record counts shapes in a pair's second word instead of measuring units, so
    # its layers stop short of the section and the groups below would drop the tail unnoticed.
    if nlayers:
        reach = max([off + ln for off, ln in table if off != 0xffff] + [nlayers])
        assert reach == geo_len, ('layers reach %d of %d units: a road-shape record, whose '
                                  'layer pairs count shapes, is not modelled here' % (reach, geo_len))
    layers = {}
    for i, (off, ln) in enumerate(table):
        if off == 0xffff: continue
        p = base + 4*off
        layers[i] = parse_group(d, p, p + 4*ln)
    sections = {}
    for name, (oi, li) in dict(names=(11, 12), s13=(13, 14), points=(15, 16), s17=(17, 18)).items():
        if w[oi] != 0xffff and w[li]:
            sections[name] = d[4*w[oi]:4*w[oi] + 4*w[li]]
        elif w[oi] != 0xffff and w[oi] != 0:
            sections[name] = b''
    return dict(hdr=w, nlayers=nlayers, layers=layers, sections=sections, gi=gi)

def build_record(r):
    w = list(r['hdr'])
    nlayers = r['nlayers']
    geo = bytearray(b'\xff\xff\x00\x00' * nlayers)
    off = nlayers
    for i in range(nlayers):
        g = r['layers'].get(i)
        if g is None: continue
        b = build_group(g)
        assert len(b) % 4 == 0
        struct.pack_into('>HH', geo, 4*i, off, len(b)//4)
        geo += b; off += len(b)//4
    out = bytearray(44)
    gi = r.get('gi', 9)
    if nlayers:
        w[gi], w[gi + 1] = 11, len(geo)//4
        out += geo
    else:
        w[gi], w[gi + 1] = 0xffff, 0
    for name, (oi, li) in dict(names=(11, 12), s13=(13, 14), points=(15, 16), s17=(17, 18)).items():
        if name in r['sections']:
            sec = r['sections'][name]
            w[oi], w[li] = len(out)//4, len(sec)//4
            out += sec
        else:
            w[oi], w[li] = 0xffff, 0
    struct.pack_into('>22H', out, 0, *w)
    return bytes(out)

if __name__ == '__main__':
    import sys, glob
    ok = bad = 0
    for fn in sys.argv[1:]:
        d = open(fn, 'rb').read()
        if struct.unpack_from('>H', d, 4)[0] != 0x19: continue
        try:
            r = parse_record(d); b = build_record(r)
            if b == d: ok += 1
            else:
                bad += 1
                if bad <= 5:
                    i = next(i for i in range(min(len(b), len(d))) if b[i] != d[i]) if b[:min(len(b),len(d))] != d[:min(len(b),len(d))] else min(len(b), len(d))
                    print('MISMATCH', fn.split('/')[-1], 'len', len(d), len(b), 'first diff at', i, d[i:i+8].hex(), b[i:i+8].hex())
        except Exception as e:
            bad += 1
            if bad <= 5: print('ERR', fn.split('/')[-1], repr(e)[:120])
    print('byte-exact rebuild ok', ok, 'bad', bad)
