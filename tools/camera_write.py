#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a list of cameras (extract_cameras.py's cameras.json) into already-built section-1
tiles' level-1 and level-2 background records. Any number of cameras, several per parcel.

    camera_write.py CAMERAS.json TILES_DIR OUT_DIR [--levels 1,2]

Each camera needs lat, lon and speed_limit_kmh, as the record's speed field is mandatory;
load_cameras fills a missing one from the road the camera sits on. direction_bearing_deg
may be null: the record then carries the factory's own undirected code, 64 ("both ways"),
instead of a guessed heading. cam_id defaults to the camera's 1-based position in the list.

TILES_DIR holds the built B*.DAT section-1 tiles (MAPAL001 naming); every file holding at
least one camera is rewritten into OUT_DIR under the same name, everything else untouched.
"""
import glob
import json
import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile
import clarion_bg as cb
import fa_encode
import rd_encode

ZWBITS = 13  # 8 KiB window, matching bg_encode.py
CAMERA = 0x0050


def find_parcel(t, lon, lat, level):
    """Directory, row, col and local x,y of the parcel at a level that covers a point -
    checks every directory at the level rather than assuming the mesh bucket (a level-2
    directory's mesh x can land a couple of units off, measured build 019)."""
    for sec in t['sections']:
        if sec['id'] != 1:
            continue
        for L in sec['levels']:
            if L['level'] != level:
                continue
            for D in L['dirs']:
                for row in range(32):
                    for col in range(32):
                        lon0, lat0, w, h = cb.parcel_geo(level, D['y'], D['x'], row, col)
                        if lon0 <= lon < lon0 + w and lat0 <= lat < lat0 + h:
                            x = max(0, min(2047, round((lon - lon0) / w * 2048)))
                            y = max(0, min(2047, round((lat - lat0) / h * 2048)))
                            return D, row, col, x, y
    return None


def get_record(D, row, col):
    k = 2 * col + 1
    sl = D['slots'][row]
    i = next(i for i, r in enumerate(sl['recs']) if r['k'] == k)
    return sl, sl['recs'][i]


def plain_of(r):
    raw = r['raw']
    return zlib.decompress(raw) if raw[:2] == b'\x58\x85' else raw


def write_record(sl, r, new_plain):
    c = zlib.compressobj(6, zlib.DEFLATED, ZWBITS, 8)
    comp = c.compress(new_plain) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    r['raw'] = comp
    r['words'] = len(new_plain) // 2
    body = bytearray(sl['body'])
    struct.pack_into('>II', body, 8 * r['k'], len(comp) // 2, len(new_plain) // 2)
    sl['body'] = bytes(body)


def direction_byte(bearing_deg):
    """64ths of a turn (samples 0..64); no bearing writes the
    factory's own undirected code, 64 ('both ways'), rather than a guess."""
    if bearing_deg is None:
        return 64
    return round(bearing_deg / 360 * 64) % 64


def speed_code(speed_kmh):
    """(code + 1) * 5 = speed, km/h. Codes 29/30 are rejected by the firmware."""
    code = round(speed_kmh / 5) - 1
    assert 0 <= code <= 28, (speed_kmh, code)
    return code


def place_one(t, cam, level, cam_id):
    found = find_parcel(t, cam['lon'], cam['lat'], level)
    if not found:
        return None
    D, row, col, x, y = found
    sl, r = get_record(D, row, col)
    rec = cb.parse_record(plain_of(r))
    direction = direction_byte(cam.get('direction_bearing_deg'))
    side = cb.camera_service_record(direction, speed_code(cam['speed_limit_kmh']), cam_id)
    s = cb.add_point(rec, CAMERA, x, y, side=side)
    write_record(sl, r, cb.build_record(rec))
    return dict(row=row, col=col, x=x, y=y, direction=direction, idx=s['idx'])


def _metres(lon, lat, a, b):
    """Distance from a point to segment a-b, flat-earth around the point."""
    kx, ky = math.cos(math.radians(lat)) * 111320, 110540
    ax, ay, bx, by = (a[0] - lon) * kx, (a[1] - lat) * ky, (b[0] - lon) * kx, (b[1] - lat) * ky
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0 if n == 0 else max(0, min(1, -(ax * dx + ay * dy) / n))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest_road(roads, lon, lat):
    """(way, metres) of the built road nearest a point, or (None, None) for no roads."""
    best = (None, None)
    for w in roads:
        pts = w['nodes']
        for a, b in zip(pts, pts[1:]):
            d = _metres(lon, lat, a, b)
            if best[1] is None or d < best[1]:
                best = (w, d)
    return best


def load_cameras(path, roads, unit='km/h'):
    """cameras.json with every speed filled in. OSM rarely tags a speed on the camera, so a
    camera without one takes the speed of the built road it sits on, the same speed the road
    links carry: the road's maxspeed, else its class default. Returns (cameras, {camera id:
    how its speed was found}) for those filled in."""
    with open(path) as f:
        listed = json.load(f)
    filled, boxes = {}, None
    for c in listed:
        if c['speed_limit_kmh'] is not None:
            continue
        if boxes is None:
            boxes = [(min(p[0] for p in w['nodes']), min(p[1] for p in w['nodes']),
                      max(p[0] for p in w['nodes']), max(p[1] for p in w['nodes'])) for w in roads]
        # only ways whose box comes within about 1 km; all of them when none does
        near = [w for w, (x0, y0, x1, y1) in zip(roads, boxes)
                if x0 - 0.02 <= c['lon'] <= x1 + 0.02 and y0 - 0.01 <= c['lat'] <= y1 + 0.01] or roads
        w, d = nearest_road(near, c['lon'], c['lat'])
        if w is None:
            raise SystemExit('camera %s has no speed and the build has no roads' % c['id'])
        c['speed_limit_kmh'] = rd_encode.speed_of(w['hw'], w.get('maxspeed'), unit)
        posted = fa_encode.parse_speed(w.get('maxspeed'), unit) is not None
        filled[c['id']] = dict(way=w['id'], name=w.get('name'), hw=w['hw'], metres=round(d, 1),
                               kmh=c['speed_limit_kmh'], source='posted' if posted else 'class default')
    return listed, filled


def write_cameras(cams, tiledir, outdir, levels=(1, 2)):
    """Writes every camera into whichever tile covers it; returns ({camera id: [placement,
    ...]}, [camera ids with no covering tile at any level])."""
    os.makedirs(outdir, exist_ok=True)
    files = [f for f in sorted(glob.glob(os.path.join(tiledir, 'B*.DAT')))
             if not f.endswith('.nosec4.DAT')]
    tiles = {os.path.basename(f): clarion_tile.read_tile(open(f, 'rb').read()) for f in files}
    touched, report, missing = set(), {}, []
    for i, cam in enumerate(cams):
        cam_id = cam.get('cam_id', i + 1)
        hits = []
        for name, t in tiles.items():
            for level in levels:
                p = place_one(t, cam, level, cam_id)
                if p:
                    touched.add(name)
                    hits.append(dict(tile=name, level=level, **p))
        report[cam['id']] = hits
        if not hits:
            missing.append(cam['id'])
    for name in touched:
        open(os.path.join(outdir, name), 'wb').write(clarion_tile.write_tile(tiles[name]))
    return report, missing


def sources(tiles, report, tiledir, outdir):
    """{tile: file to use}: the written tile for those write_cameras' report says it touched,
    the input tile for the rest, whatever else is in outdir."""
    touched = {h['tile'] for hits in report.values() for h in hits}
    return {name: os.path.join(outdir if '%s.DAT' % name in touched else tiledir, '%s.DAT' % name)
            for name in tiles}


def main(argv):
    cj, tiledir, outdir = argv[0], argv[1], argv[2]
    levels = tuple(int(x) for x in
                    (argv[argv.index('--levels') + 1].split(',') if '--levels' in argv else ('1', '2')))
    report, missing = write_cameras(json.load(open(cj)), tiledir, outdir, levels)
    for cid, hits in report.items():
        print(cid, hits)
    if missing:
        print('no covering tile for:', missing)


if __name__ == '__main__':
    main(sys.argv[1:])
