#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Widen the level-6 background of LEV3456R.DAT to the west.

Level 6 is one raw record for the whole database (box 904..992 x 32..120 meshes,
2048 units each way). Every shape is rescaled into the wider box, a sea polygon for
the new strip (minus OSM land) is added, and the record is rewritten at the end of the
file with its directory header, slot and section-table box updated.

    ./lev_l6.py --profile iceland IN.DAT OUT.DAT
    --coastline PATH   overrides the profile's
    --west-meshes N    meshes added on the west
    --shift            move the box west instead of widening it: shapes are shifted and
                       clipped on the east. Widening the box with the record left at 904
                       blanks level 6 from 640 km to 10 km.
    --points TYPE:lon,lat;...   label points (layer 25) after the move, e.g. country codes
    --labels TYPE:FLAGS:lon,lat:TEXT[:ID];...   text labels (names section)
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, bg_encode, bg_tiles, clarion_profile
from clarion_tile import addr_of, enc_addr, pad256
from clarion_bg import mesh_to_lon, mesh_to_lat
from shapely.geometry import box, Polygon, LineString

SEA, DIR = 0x8012, 0x1300

def clip_east(kind, shapes, dx):
    """Shift shapes east by dx units and cut off what leaves the 0..2047 box."""
    clip = box(0, 0, 2047, 2047); out = []
    for s in shapes:
        pts = [(x + dx, y) for x, y in s['pts']]
        if max(x for x, _ in pts) <= 2047:
            out.append(dict(s, pts=[(round(x), y) for x, y in pts])); continue
        if kind == 0: continue
        if kind == 0x80:
            g = Polygon(pts)
            if not g.is_valid: g = g.buffer(0)
            g = g.intersection(clip)
            for p in (g.geoms if hasattr(g, 'geoms') else [g]):
                if p.geom_type != 'Polygon' or p.area < 1: continue
                ring = list(p.exterior.coords)
                if p.exterior.is_ccw: ring.reverse()
                q = [(max(0, min(2047, round(x))), max(0, min(2047, round(y)))) for x, y in ring]
                q = [q[0]] + [b for a, b in zip(q, q[1:]) if b != a]
                out.append(dict(s, pts=bg_encode.densify(q)))
        else:
            g = LineString(pts).intersection(clip)
            for p in (g.geoms if hasattr(g, 'geoms') else [g]):
                if p.geom_type != 'LineString' or p.length < 1: continue
                q = [(max(0, min(2047, round(x))), max(0, min(2047, round(y)))) for x, y in p.coords]
                q = [q[0]] + [b for a, b in zip(q, q[1:]) if b != a]
                if len(q) > 1: out.append(dict(s, pts=bg_encode.densify(q)))
    return out

def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    bg_tiles.use_profile(P)
    west, shift = P.west_meshes, P.shift
    coast_path = P.coastline
    labels = bg_encode.parse_labels(P.coarse_labels) if P.coarse_labels else []
    if '--shift' in argv: argv.remove('--shift'); shift = True
    if '--no-shift' in argv: argv.remove('--no-shift'); shift = False; west = 0
    if '--coastline' in argv:
        i = argv.index('--coastline'); coast_path = argv[i+1]; del argv[i:i+2]
    if '--labels' in argv:
        i = argv.index('--labels'); labels = bg_encode.parse_labels(argv[i+1]); del argv[i:i+2]
    points = []
    if '--points' in argv:
        i = argv.index('--points')
        for item in argv[i+1].split(';'):
            typ, ll = item.split(':'); lon, lat = ll.split(','); points.append((int(typ, 0), float(lon), float(lat)))
        del argv[i:i+2]
    if '--west-meshes' in argv: i = argv.index('--west-meshes'); west = int(argv[i+1]); del argv[i:i+2]
    src, dest = argv[0], argv[1]
    if len(argv) > 2: coast_path = argv[2]
    d = bytearray(open(src, 'rb').read())
    nw = struct.unpack_from('>H', d, 0x1000)[0]; w = list(struct.unpack_from('>%dH' % nw, d, 0x1000))
    o = w[1]; y1, y0, x0, x1, z, t = w[o+2:o+8]; a = addr_of((z << 16) | t)
    assert a == DIR and d[a+10:a+12] == b'\x00\x80', 'level-6 directory not where expected'
    tag = struct.unpack_from('>I', d, a + 12)[0]; ln = struct.unpack_from('>I', d, a + 16)[0]
    pl = bytes(d[addr_of(tag):addr_of(tag) + 2*ln]); assert pl[:2] != b'\x58\x85'
    rec = clarion_bg.parse_record(pl)
    old_w = x1 - x0; new_w = old_w if shift else old_w + west
    nx0, nx1 = x0 - west, (x1 - west if shift else x1)
    scale, dx = old_w / new_w, 2048 * west / new_w
    def rx(x): return max(0, min(2047, round(x * scale + dx)))
    for g in rec['layers'].values():
        assert 'raw' not in g, 'unparsed layer'
        for sub in [g] + g.get('more', []):
            if not shift:
                for s in sub['shapes']: s['pts'] = [(rx(x), y) for x, y in s['pts']]
            else:
                sub['shapes'] = clip_east(sub['kind'], sub['shapes'], dx)
    # sea for the new strip, land cut out
    land, coast, tree = bg_tiles.load_land(coast_path)
    lon0, lat0, wd, hd = mesh_to_lon(nx0), mesh_to_lat(y0), float(new_w), (y1 - y0) / 1.5
    x1 = nx1
    strip = box(lon0, lat0, lon0 + west, lat0 + hd)
    sea = strip.difference(bg_tiles.clean_land(land, 6)[0])
    added = 0
    for p in (sea.geoms if hasattr(sea, 'geoms') else [sea]):
        if p.geom_type != 'Polygon' or p.area < 1e-3: continue
        s = bg_encode.ring_shape(p.exterior, lon0, lat0, wd, hd, SEA)
        if s: rec['layers'][1]['shapes'].append(s); added += 1
    for typ, lon, lat in points:
        x = round((lon - lon0) / wd * 2048); y = round((lat - lat0) / hd * 2048)
        rec['layers'][25]['shapes'].append(dict(type=typ, ref=0xffff, pts=[(x, y)]))
    if labels:
        rec['sections']['names'] = bg_encode.name_section(bg_encode.labels_in(labels, lon0, lat0, wd, hd))
    rec['hdr'][0], rec['hdr'][1] = clarion_bg.parcel_code(6, 2 * y0, 2 * nx0)
    new_pl = clarion_bg.build_record(rec)
    if len(new_pl) % 2: new_pl += b'\0'
    pos = pad256(len(d)); d += b'\0' * (pos - len(d)); d += new_pl; d += b'\0' * (pad256(len(d)) - len(d))
    struct.pack_into('>I', d, a + 12, enc_addr(pos)); struct.pack_into('>I', d, a + 16, len(new_pl) // 2)
    struct.pack_into('>H', d, a + 8, nx0)                 # directory header x (absolute at this level)
    w[o+4], w[o+5] = nx0, nx1; struct.pack_into('>%dH' % nw, d, 0x1000, *w)
    open(dest, 'wb').write(d)
    print(f'level 6: box x {x0}..{x1} -> {nx0}..{x1}, record {len(pl)} -> {len(new_pl)} bytes at {pos:#x}, sea shapes added {added}, code {[hex(v) for v in rec["hdr"][:2]]}')

if __name__ == '__main__':
    main()
