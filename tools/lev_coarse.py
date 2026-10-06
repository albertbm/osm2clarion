#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-database coarse directories (levels 4 and 5) in LEV3456R.DAT: widen them to the
west and fill new columns with parcels built from OSM.

Level 4: 2x2-mesh parcels, two pairs per column (background at 2*col+1), record kind 0x3e8.
Level 5: 8x8-mesh parcels, one pair per column, record kind 0xfa (0xfa0 when empty). Both use 20'/30' code
units and type prefix 0x80xx. Rows are never changed. Rebuilt directories and row blocks
are written at the end of the file; the section table entry (box, address) is updated.

    ./lev_coarse.py --profile iceland IN.DAT OUT.DAT [CELL...]
    --coastline PATH   overrides the profile's
    --west-meshes N    meshes added on the west
    --shift            keep the column count: drop as many columns on the east as are
                       added on the west. The unit assumes 44 level-4 and 11 level-5
                       columns and never reads the count, so a widened one is refused.
    --bounds latmax,latmin,lonmin,lonmax
    --labels TYPE:FLAGS:lon,lat:TEXT[:ID];...   text labels for the rebuilt parcels (level 5 only)
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clarion_tile import addr_of, enc_addr, pad256
from shapely.geometry import box
from shapely.strtree import STRtree
from shapely.ops import linemerge, unary_union
from shapely.geometry import MultiLineString, LineString
import bg_encode, bg_tiles, clarion_profile
from clarion_bg import tile_cell, mesh_to_lon, mesh_to_lat, LEVELS

CFG = {4: dict(group=3, meshes=2, ppc=2, kinds=(0x3e8,)), 5: dict(group=2, meshes=8, ppc=1, kinds=(0xfa, 0xfa0))}

def read_dir(d, level):
    nw = struct.unpack_from('>H', d, 0x1000)[0]; w = struct.unpack_from('>%dH' % nw, d, 0x1000)
    o = w[CFG[level]['group']]; y1, y0, x0, x1, z, t = w[o+2:o+8]; a = addr_of((z << 16) | t)
    size2, cols, rows, hy, hx, typ = struct.unpack_from('>6H', d, a)
    body = cols * CFG[level]['ppc'] * 8
    assert size2 * 2 == 12 + rows * (4 + body), (level, size2, cols, rows, body)
    slots = []
    p = a + 12
    for r in range(rows):
        tag = struct.unpack_from('>I', d, p)[0]
        pairs = list(struct.unpack_from('>%dI' % (body // 4), d, p + 4))
        recs = [None] * (body // 8)
        if tag != 0xffffffff:
            q = addr_of(tag)
            for k in range(body // 8):
                ln, X = pairs[2*k], pairs[2*k+1]
                if ln == 0xffffffff: continue
                n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X)
                recs[k] = bytes(d[q:q+n]); q += n
        slots.append(dict(tag=tag, pairs=pairs, recs=recs))
        p += 4 + body
    return dict(level=level, table_off=o, box=[y1, y0, x0, x1], off=a, cols=cols, rows=rows, typ=typ, hy=hy, hx=hx, slots=slots)

def write_dir(d, D):
    """Append the directory (and any row block whose records changed) at the end; patch the table."""
    level = D['level']; ppc = CFG[level]['ppc']; body = D['cols'] * ppc * 8
    out = bytearray(); pos0 = pad256(len(d))
    dsize = 12 + D['rows'] * (4 + body)
    hdr = struct.pack('>6H', dsize // 2, D['cols'], D['rows'], D['hy'], (D['box'][2] - 1024) & 0xffff, D['typ'])  # x is stored as x0 - 1024
    out += hdr
    blocks = bytearray(); bpos = pad256(pos0 + dsize)
    for s in D['slots']:
        if s.get('dirty'):
            block = b''.join(r for r in s['recs'] if r is not None)
            tag = enc_addr(bpos + len(blocks)); blocks += block; blocks += b'\0' * (pad256(len(blocks)) - len(blocks))
        else:
            tag = s['tag']
        out += struct.pack('>I', tag) + struct.pack('>%dI' % (body // 4), *s['pairs'])
    out += b'\0' * (bpos - pos0 - len(out)); out += blocks
    d += b'\0' * (pos0 - len(d)); d += out
    # section table entry
    nw = struct.unpack_from('>H', d, 0x1000)[0]; w = list(struct.unpack_from('>%dH' % nw, d, 0x1000))
    o = D['table_off']; a = enc_addr(pos0)
    w[o+2:o+8] = [D['box'][0], D['box'][1], D['box'][2], D['box'][3], a >> 16, a & 0xffff]
    struct.pack_into('>%dH' % nw, d, 0x1000, *w)

def widen_west(D, meshes, shift=False):
    """Add meshes/parcel-size columns on the west; existing pairs move right, blocks untouched.
    With shift the same number of columns is dropped on the east, so the size stays."""
    add = meshes // CFG[D['level']]['meshes']; ppc = CFG[D['level']]['ppc']
    for s in D['slots']:
        s['pairs'] = [0xffffffff] * (add * ppc * 2) + s['pairs']
        s['recs'] = [None] * (add * ppc) + s['recs']
        if shift:
            s['pairs'] = s['pairs'][:len(s['pairs']) - add * ppc * 2]; s['recs'] = s['recs'][:len(s['recs']) - add * ppc]
            s['dirty'] = True
    D['box'][2] -= meshes
    if shift: D['box'][3] -= meshes
    else: D['cols'] += add
    return [(r, c) for r in range(D['rows']) for c in range(add)]

def cell_parcels(D, cells):
    m = CFG[D['level']]['meshes']; y0, x0 = D['box'][1], D['box'][2]; out = set()
    for name in cells:
        yb, xb = tile_cell(name)
        for yy in range(yb, yb + 8, m):
            for xx in range(xb, xb + 8, m):
                row, col = (yy - y0) // m, (xx - x0) // m
                if 0 <= row < D['rows'] and 0 <= col < D['cols']: out.add((row, col))
    return sorted(out)

def fill(D, parcels, land, coast, tree, replace=(), labels=()):
    """Build a background record for each (row, col) that has none yet, or is in replace."""
    level = D['level']; m = CFG[level]['meshes']; ppc = CFG[level]['ppc']
    y0, x0 = D['box'][1], D['box'][2]
    from shapely.prepared import prep
    pland = prep(land)
    added = 0
    for row, col in sorted(set(parcels)):
        k = col * ppc + (ppc - 1)
        if D['slots'][row]['recs'][k] is not None and (row, col) not in replace: continue
        yy, xx = y0 + m * row, x0 + m * col
        lon0, lat0, wd, hd = mesh_to_lon(xx), mesh_to_lat(yy), float(m), m * 2 / 3
        rect = box(lon0, lat0, lon0 + wd, lat0 + hd)
        sea_polys, coastl = [], []
        if not pland.intersects(rect):
            sea_polys = [dict(type=LEVELS[level]['seatype'], ref=0xffff, pts=bg_encode.densify([(0, 0), (0, 2047), (2047, 2047), (2047, 0), (0, 0)]))]
        elif pland.contains(rect):
            pass
        else:
            sea = rect.difference(land)
            for p in (sea.geoms if hasattr(sea, 'geoms') else [sea]):
                if p.geom_type == 'Polygon' and not p.is_empty:
                    s = bg_encode.ring_shape(p.exterior, lon0, lat0, wd, hd, LEVELS[level]['seatype'])
                    if s: sea_polys.append(s)
            pieces = []
            for i in tree.query(rect):
                inter = coast[i].intersection(rect)
                if not inter.is_empty: pieces += [g for g in (inter.geoms if hasattr(inter, 'geoms') else [inter]) if g.geom_type == 'LineString']
            if pieces:
                merged = linemerge(MultiLineString(pieces)) if len(pieces) > 1 else pieces[0]
                coastl = bg_encode.line_shapes(merged, lon0, lat0, wd, hd, LEVELS[level]['coasttype'])
            sea_polys = sorted(sea_polys, key=lambda s: -len(s['pts']))[:255]; coastl = sorted(coastl, key=lambda s: -len(s['pts']))[:255]
        labs = bg_encode.labels_in(labels, lon0, lat0, wd, hd) if level == 5 else []
        plain, comp = bg_encode.build_parcel_record(level, y0, x0, row, col, sea_polys, coastl, labs)
        s = D['slots'][row]; s['recs'][k] = comp; s['pairs'][2*k], s['pairs'][2*k+1] = len(comp)//2, len(plain)//2; s['dirty'] = True
        added += 1
    return added

def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    bg_tiles.use_profile(P)
    west, bounds, shift = P.west_meshes, P.bounds, P.shift
    labels = bg_encode.parse_labels(P.coarse_labels) if P.coarse_labels else []
    coast_path = P.coastline
    if '--shift' in argv: argv.remove('--shift'); shift = True
    if '--no-shift' in argv: argv.remove('--no-shift'); shift = False; west = 0
    if '--labels' in argv:
        i = argv.index('--labels'); labels = bg_encode.parse_labels(argv[i+1]); del argv[i:i+2]
    if '--coastline' in argv:
        i = argv.index('--coastline'); coast_path = argv[i+1]; del argv[i:i+2]
    if '--west-meshes' in argv:
        i = argv.index('--west-meshes'); west = int(argv[i+1]); del argv[i:i+2]
    if '--bounds' in argv:
        i = argv.index('--bounds'); bounds = tuple(int(v) for v in argv[i+1].split(',')); del argv[i:i+2]
    src, dest = argv[0], argv[1]
    cells = argv[2:] or P.cells
    d = bytearray(open(src, 'rb').read())
    land, coast, tree = bg_tiles.load_land(coast_path)
    for level in (5, 4):
        D = read_dir(d, level)
        parcels = widen_west(D, west, shift) if west else []
        # The named cells are rebuilt outright from the cleaned outline for this level:
        # the shipped parcels there are sea only, even where they exist.
        own = set(cell_parcels(D, cells))
        n = fill(D, parcels + sorted(own), *bg_tiles.clean_land(land, level), replace=own, labels=labels)
        write_dir(d, D)
        print(f'level {level}: {D["cols"]} columns, box {D["box"]}, parcels written {n}')
    if bounds:
        old_b = struct.unpack_from('>hhhh', d, 0x184)
        struct.pack_into('>hhhh', d, 0x184, *bounds); struct.pack_into('>hh', d, 0x5d0, bounds[1], bounds[2])
        # Per-level boxes at 0x2ac: per section, entries of (level, flag, y1, y0, x0, x1).
        # Under --shift the whole grid moved, so every bound moves with it; widening only
        # moves the bounds that sit on the old edge.
        delta = bounds[2] - old_b[2]
        p = 0x2ac + 2
        for _ in range(struct.unpack_from('>H', d, 0x2ac)[0]):
            sec, cnt = d[p], d[p + 1]; p += 2
            for _ in range(cnt):
                for i, name in ((6, 'lonmin'), (8, 'lonmax')):
                    was = struct.unpack_from('>h', d, p + i)[0]
                    if shift: now = was + delta
                    elif was == old_b[i // 2 - 1]: now = bounds[i // 2 - 1]
                    else: continue
                    struct.pack_into('>h', d, p + i, now); print(f'section {sec} level {d[p]} box: {name} {was} -> {now}')
                p += 10
    open(dest, 'wb').write(d); print('wrote', dest, len(d))


if __name__ == '__main__':
    main()
