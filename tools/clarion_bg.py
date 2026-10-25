# SPDX-License-Identifier: GPL-3.0-or-later
"""Background (section 1) record: parse to a dict and build back to bytes.

Header (big-endian, unit = 4 bytes): w0,w1 parcel code, w2 type (0x19), w7..w20 seven
(offset,length) unit pairs (w9,w10 geometry, w11,w12 names, w15,w16 point index). Geometry
at unit 11: layer table of (offset,length) pairs, then groups of 0001 0003, kind byte (0x80
polygon, 0x40 polyline, 0x00 point), count byte, shapes (nvertices, type, ref, x0, y0, int8
dx,dy deltas). parse_record refuses a road-shape record (w2=0x00fa, counts shapes not units)
rather than return it short of its tail.
"""
import struct

MAGIC = b'\x00\x01\x00\x03'

# Mesh coordinates: y = lat * 1.5, x = lon + 924 (a mesh is 1 deg x 40').
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

# OSM kind -> (layer, class); class is the low 12 bits of the shape type, read off factory
# tile B48R060R (Koeln): 01a forest, 01d park, 015 lake, 014 river.
AREA_CLASSES = {'forest': (3, 0x01a), 'heath': (3, 0x01b), 'park': (3, 0x01d),
                'water': (7, 0x015), 'river': (7, 0x014)}

def tile_cell(name):
    """South-west mesh (y0, x0) of tile BbbRrrr: filenames are hexadecimal."""
    b = int(name[1:3], 16); r = int(name[4:7], 16)
    return b, 1024 - r

def mesh_to_lon(x): return x - 924
def mesh_to_lat(y): return y / 1.5

def parcel_geo(level, y0, x0, row, col):
    """Lower-left corner and size in degrees of a parcel in the directory at (y0, x0)."""
    L = LEVELS[level]
    w = MESH_W * L['dir_meshes'] / L['cols']; h = MESH_H * L['dir_meshes'] / L['rows']
    return mesh_to_lon(x0) + col * w, mesh_to_lat(y0) + row * h, w, h


def s8(b): return b - 256 if b > 127 else b

def parcel_code(level, row, col):
    """w0, w1 for a parcel at absolute (row, col)."""
    if level in (3, 4, 5, 6):   # 20' rows, 30' cols (2/mesh); level 4 parcels are 2x2 meshes, level 5 8x8
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
    """A layer: sub-group count, header word count, then per sub-group a kind byte, count
    byte and (all but the last) the word offset where it ends; extras kept as 'more'."""
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

def camera_point_group(points):
    """Layer-25 point group; points: [(x, y, idx)]. Firmware reads only the first
    subgroup (a stride bug), so every point goes in that one."""
    assert 1 <= len(points) <= 0xff
    body = b''.join(struct.pack('>HHHHH', 0x0050, 0xffff, x, 0x8000 | y, idx) for x, y, idx in points)
    return struct.pack('>HHH', 1, 3, len(points)) + body

def camera_service_record(direction, speed_code, cam_id, mph=False, cam_type=0):
    """One 10-word VFI_setData camera record; speed_code: real speed is (code + 1) * 5."""
    w6 = 0x80 | (0x40 if mph else 0) | 0x20 | (speed_code & 0x1f) | ((cam_type & 7) << 13)
    return struct.pack('>10H', 0, direction, 0xffff, 1, 0xc000, 4, w6, 0, cam_id >> 16, cam_id & 0xffff)

def emblem_point_group(points):
    """Layer-25 point group for a logo marker (type 0x00ff); idx points at a 12-byte
    id entry (emblem_entry), not a service record."""
    assert 1 <= len(points) <= 0xff
    body = b''.join(struct.pack('>HHHHH', 0x00ff, 0xffff, x, 0x8000 | y, idx) for x, y, idx in points)
    return struct.pack('>HHH', 1, 3, len(points)) + body

def emblem_entry(emblem_id):
    """12-byte point-index entry for a logo marker; id 0x193 = Nissan (factory B48R068R)."""
    return struct.pack('>HHHHHH', 0, 0, 0xffff, 0, 0x2000 | emblem_id, 0)

def bg_slot(w):
    """Which pointer pair holds a record's geometry: w9/w10 normally, w7/w8 for a
    road-only record. A level-5 record has w2=0xfa either way, so this reads the
    pointers rather than w2; w9/w10 wins when both are filled."""
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
    # a road-shape record counts shapes, not units, so its layers stop short and would drop the tail unnoticed
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
