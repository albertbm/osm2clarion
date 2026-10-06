#!/usr/bin/env python3
"""Coarse roads in LEV3456R.DAT: levels 3 and 4.

Two jobs. First the section-2 level-4 and level-5 road directories are moved west, as the
backgrounds were -- their box still said 904..992 after lev_coarse ran. Then road-shape
records (kind 0x00fa, the even pair next to the background record) go into the level-4 and
level-3 parcels. No graph records: both levels draw without them, and a graph is routing
data that nothing on this card reads at those levels.

    ./lev_roads.py --profile iceland IN.DAT OUT.DAT [roads.json]
    --classes motorway,trunk,primary   overrides the profile's coarse set
    --simplify METRES                  level-4 tolerance; level 3 uses a quarter of it
"""
import json, math, os, sys, struct, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lev_coarse, clarion_bg, clarion_profile, rd_build
from clarion_tile import addr_of, enc_addr, pad256

L4_TAIL = 0x0318         # per-parcel token seen in a shipped level-4 pair; any value, same in both
L3_TAIL = 0x02d4
PW, PH = 2.0, 4 / 3      # level-4 parcel in degrees (2x2 meshes)
PW3, PH3 = 0.5, 1 / 3    # level-3 parcel in degrees (30' x 20')

# South-west corner of the database grid, in degrees and in meshes. Set from a profile.
LON0, LAT0 = -20.0, 32 / 1.5
Y0, X0 = 32, 904

CLASS_CODE = {'motorway': 3, 'motorway_link': 3, 'trunk': 4, 'trunk_link': 4,
              'primary': 5, 'primary_link': 5, 'secondary': 6, 'secondary_link': 6}
CLASS_CODE3 = dict(CLASS_CODE, tertiary=7, tertiary_link=7)


def use_profile(P):
    global LON0, LAT0, Y0, X0
    Y0, X0 = P.mesh_origin
    LON0, LAT0 = clarion_bg.mesh_to_lon(X0), clarion_bg.mesh_to_lat(Y0)


def dir_of_section2(d, slot):
    a2 = addr_of(struct.unpack_from('>I', d, 0x806)[0]); nw = struct.unpack_from('>H', d, a2)[0]
    w = list(struct.unpack_from('>%dH' % nw, d, a2)); o = w[slot]
    return a2, nw, w, o


def shift_section2(d, slot, ncol, meshes):
    """Move a whole-database road directory ncol columns (meshes in all) west; rebuilt at
    the end of the file."""
    a2, nw, w, o = dir_of_section2(d, slot)
    y1, y0, x0, x1, z, t = w[o+2:o+8]; a = addr_of((z << 16) | t)
    size2, cols, rows, hy, hx, typ = struct.unpack_from('>6H', d, a); body = cols * 8
    slots = []; p = a + 12
    for r in range(rows):
        tag = struct.unpack_from('>I', d, p)[0]
        pairs = list(struct.unpack_from('>%dI' % (body // 4), d, p + 4)); recs = [None] * cols
        if tag != 0xffffffff:
            q = addr_of(tag)
            for k in range(cols):
                ln, X = pairs[2*k], pairs[2*k+1]
                if ln == 0xffffffff: continue
                n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X); recs[k] = bytes(d[q:q+n]); q += n
        pairs = [0xffffffff, 0] * ncol + pairs[:-2 * ncol]; recs = [None] * ncol + recs[:-ncol]
        slots.append((tag, pairs, recs)); p += 4 + body
    nx0, nx1 = x0 - meshes, x1 - meshes
    pos0 = pad256(len(d))
    out = bytearray(struct.pack('>6H', size2, cols, rows, hy, (nx0 - 1024) & 0xffff, typ))
    dsize = 12 + rows * (4 + body); blocks = bytearray(); bpos = pad256(pos0 + dsize)
    for tag, pairs, recs in slots:
        if tag == 0xffffffff:
            out += struct.pack('>I', tag) + struct.pack('>%dI' % (body // 4), *pairs); continue
        block = b''.join(r for r in recs if r is not None)
        out += struct.pack('>I', enc_addr(bpos + len(blocks))) + struct.pack('>%dI' % (body // 4), *pairs)
        blocks += block; blocks += b'\0' * (pad256(len(blocks)) - len(blocks))
    out += b'\0' * (bpos - pos0 - len(out)); out += blocks
    d += b'\0' * (pos0 - len(d)); d += out
    a = enc_addr(pos0); w[o+4], w[o+5], w[o+6], w[o+7] = nx0, nx1, a >> 16, a & 0xffff
    struct.pack_into('>%dH' % nw, d, a2, *w)
    print(f'section 2 slot {slot}: {rows}x{cols} moved to x {nx0}..{nx1}, directory at {pos0:#x}')


def shape_words_l4(pts, cls):
    """The unnamed shipped form: class in the top nibble of word 2, word 3 0x3000; start
    point, then per link an id and its end point (middle 0x2000|x, 0x4800|y; last
    0x3000|x, 0x4000|y)."""
    body = [0x8000 | len(pts), cls << 12, 0x3000, 0xffff, 0, 0, 0x0800 | pts[0][0], 0x4800 | pts[0][1]]
    for i, (x, y) in enumerate(pts[1:], start=1):
        last = i == len(pts) - 1
        body += [i, (0x3000 if last else 0x2000) | x, (0x4000 if last else 0x4800) | y]
    return [len(body) + 1] + body


def shape_words_l3(pts, cls):
    """Level-3 road shape: 0x4000|points, class, 0, no name, 0, 0, then the points
    (start 0x0800|x)."""
    body = [0x4000 | len(pts), cls << 12, 0, 0xffff, 0, 0]
    for i, (x, y) in enumerate(pts): body += [(0x0800 if i == 0 else 0x1000) | x, y]
    return [len(body) + 1] + body


def fa_record(level, code, shapes, tail):
    """A road-shape record with its shapes in layer 6 -- where every shipped coarse record
    of classes 3 to 6 keeps them. Layer 5 is empty throughout the shipped data."""
    unit = [0xffff, 0x0000] * 11
    unit[12], unit[13] = 11, len(shapes)          # layer 6, data at dword 11
    for s in shapes: unit += s
    if len(unit) % 2: unit.append(0)
    w = [code[0], code[1], 0x00fa, 0xffff, 0xffff, 0, 0, 11, len(unit) // 2,
         0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0, tail, 0]
    plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(plain) + c.flush()
    if len(comp) % 2: comp += b'\0'
    return plain, comp


def parcel4(lon, lat):
    return (int((lat - LAT0) // PH), int((lon - LON0) // PW))


def quant4(lon, lat, row, col):
    lon0, lat0 = LON0 + col * PW, LAT0 + row * PH
    return (max(0, min(2047, round((lon - lon0) / PW * 2048))),
            max(0, min(2047, round((lat - lat0) / PH * 2048))))


def parcel3(lon, lat):
    """(cell y0, cell x0, row, col) of a level-3 parcel: 16 rows by 16 columns to a cell."""
    r = int((lat - LAT0) // PH3); c = int((lon - LON0) // PW3)
    return (Y0 + 8 * (r // 16), X0 + 8 * (c // 16), r % 16, c % 16)


def quant3(lon, lat, pc):
    lon0, lat0, w, h = clarion_bg.parcel_geo(3, *pc)
    return (max(0, min(2047, round((lon - lon0) / w * 2048))),
            max(0, min(2047, round((lat - lat0) / h * 2048))))


def runs(ways, PW, PH, parcel, quant, maxpts=200):
    """Per parcel, the polyline (in parcel units) of each way's run inside it."""
    parcels = {}
    for wy in ways:
        pts = wy['nodes']; cur = None; run = []
        def flush():
            if cur is not None and len(run) > 1:
                for i in range(0, len(run) - 1, maxpts - 1):
                    piece = run[i:i + maxpts]
                    if len(piece) > 1: parcels.setdefault(cur, []).append((piece, wy['hw']))
        for a, b in zip(pts, pts[1:]):
            seg = [a]; cr = []
            for k in range(1, 50):
                lon = (math.floor((min(a[0], b[0]) - LON0) / PW) + k) * PW + LON0
                if lon >= max(a[0], b[0]): break
                if b[0] != a[0]: cr.append((lon - a[0]) / (b[0] - a[0]))
            for k in range(1, 50):
                lat = (math.floor((min(a[1], b[1]) - LAT0) / PH) + k) * PH + LAT0
                if lat >= max(a[1], b[1]): break
                if b[1] != a[1]: cr.append((lat - a[1]) / (b[1] - a[1]))
            for t in sorted(c for c in cr if 0 < c < 1):
                seg.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            seg.append(b)
            for p, q in zip(seg, seg[1:]):
                pc = parcel((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                if pc != cur:
                    flush(); cur = pc; run = [quant(*p, pc)]
                e = quant(*q, pc)
                if e != run[-1]: run.append(e)
        flush()
    return parcels


def add_level3(d, parcels):
    """Even-pair shape records into the level-3 cell directories. Each cell that is touched
    is rewritten at the end of the file and its section-1 entry re-pointed."""
    a1 = addr_of(struct.unpack_from('>I', d, 0x800)[0]); nw = struct.unpack_from('>H', d, a1)[0]
    w = list(struct.unpack_from('>%dH' % nw, d, a1))
    o = w[4]; cnt = w[o + 1]; entries = {}
    for i in range(cnt):
        y1, y0, x0, x1 = w[o+2+6*i:o+6+6*i]; entries[(y0, x0)] = i
    by_cell = {}
    for (y0, x0, row, col), segs in parcels.items():
        by_cell.setdefault((y0, x0), {}).setdefault(row, {})[col] = segs
    added = 0
    for cell, rows in sorted(by_cell.items()):
        if cell not in entries: continue
        i = entries[cell]; z, t = w[o+6+6*i], w[o+7+6*i]; a = addr_of((z << 16) | t)
        hdr = d[a:a+12]; body = 256; slots = []; p = a + 12
        for r in range(16):
            tag = struct.unpack_from('>I', d, p)[0]
            pairs = list(struct.unpack_from('>64I', d, p + 4)); recs = [None] * 32
            if tag != 0xffffffff:
                q = addr_of(tag)
                for k in range(32):
                    ln, X = pairs[2*k], pairs[2*k+1]
                    if ln in (0xffffffff, 0x7fffffff): continue
                    n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X); recs[k] = bytes(d[q:q+n]); q += n
            slots.append([tag, pairs, recs, False]); p += 4 + body
        for row, cols in rows.items():
            for col, segs in cols.items():
                if slots[row][2][2 * col + 1] is None: continue   # no background record here
                shapes = [shape_words_l3(pts, c) for pts, c in segs]
                code = clarion_bg.parcel_code(3, 2 * cell[0] + row, 2 * cell[1] + col)
                plain, comp = fa_record(3, code, shapes, L3_TAIL)
                slots[row][2][2 * col] = comp
                slots[row][1][4 * col], slots[row][1][4 * col + 1] = len(comp) // 2, len(plain) // 2
                slots[row][3] = True; added += 1
        pos0 = pad256(len(d)); out = bytearray(hdr); dsize = 12 + 16 * (4 + body)
        blocks = bytearray(); bpos = pad256(pos0 + dsize)
        for tag, pairs, recs, dirty in slots:
            if dirty:
                block = b''.join(r for r in recs if r is not None)
                tag = enc_addr(bpos + len(blocks))
                blocks += block; blocks += b'\0' * (pad256(len(blocks)) - len(blocks))
            out += struct.pack('>I', tag) + struct.pack('>64I', *pairs)
        out += b'\0' * (bpos - pos0 - len(out)); out += blocks
        d += b'\0' * (pos0 - len(d)); d += out
        na = enc_addr(pos0); w[o+6+6*i], w[o+7+6*i] = na >> 16, na & 0xffff
    struct.pack_into('>%dH' % nw, d, a1, *w)
    return added


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    classes = P.coarse_classes
    tol = P.coarse_simplify_metres
    west = P.west_meshes
    if '--classes' in argv:
        i = argv.index('--classes'); classes = set(argv[i+1].split(',')); del argv[i:i+2]
    if '--simplify' in argv:
        i = argv.index('--simplify'); tol = float(argv[i+1]); del argv[i:i+2]
    if '--west-meshes' in argv:
        i = argv.index('--west-meshes'); west = int(argv[i+1]); del argv[i:i+2]
    src, dest = argv[0], argv[1]
    rpath = argv[2] if len(argv) > 2 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    d = bytearray(open(src, 'rb').read())
    if west:
        shift_section2(d, 1, west // 8, west)    # level 5, 8-mesh columns
        shift_section2(d, 2, west // 2, west)    # level 4, 2-mesh columns

    ways = [w for w in json.load(open(rpath)) if w['hw'] in classes]
    for w in ways: w['nodes'] = rd_build.simplify(w['nodes'], tol)
    parcels = runs(ways, PW, PH, parcel4, lambda lon, lat, pc: quant4(lon, lat, *pc))
    D = lev_coarse.read_dir(d, 4)
    assert D['box'][2] == X0, (D['box'], X0)
    added = 0
    for (row, col), rl in sorted(parcels.items()):
        if not (0 <= row < D['rows'] and 0 <= col < D['cols']): continue
        shapes = [shape_words_l4(pts, CLASS_CODE.get(hw, 6)) for pts, hw in rl]
        code = clarion_bg.parcel_code(4, 2 * (Y0 + 2 * row), 2 * (X0 + 2 * col))
        plain, comp = fa_record(4, code, shapes, L4_TAIL)
        s = D['slots'][row]; k = 2 * col
        if s['recs'][k + 1] is None: continue     # no background record here: leave it
        s['recs'][k] = comp
        s['pairs'][2*k], s['pairs'][2*k+1] = len(comp) // 2, len(plain) // 2
        s['dirty'] = True; added += 1
    lev_coarse.write_dir(d, D)
    print(f'level-4 shape records added: {added} parcels, '
          f'{sum(len(v) for v in parcels.values())} segments')

    ways3 = [w for w in json.load(open(rpath)) if w['hw'] in CLASS_CODE3]
    for w in ways3: w['nodes'] = rd_build.simplify(w['nodes'], tol / 4)
    p3 = runs(ways3, PW3, PH3, parcel3, quant3)
    n3 = add_level3(d, {pc: [(pts, CLASS_CODE3.get(hw, 7)) for pts, hw in rl]
                        for pc, rl in p3.items()})
    print(f'level-3 shape records added: {n3}')
    open(dest, 'wb').write(d)
    print(f'wrote {dest} {len(d)} bytes')


if __name__ == '__main__':
    main()
