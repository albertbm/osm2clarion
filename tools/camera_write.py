#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a list of cameras (extract_cameras.py's cameras.json) into already-built section-1
tiles' level-1 and level-2 background records. Any number of cameras, several per parcel.

    camera_write.py CAMERAS.json TILES_DIR OUT_DIR [--levels 1,2]

Each camera needs lat, lon and speed_limit_kmh (the record's speed field is mandatory;
filling in a missing one, from the nearest road's own limit or otherwise, is the caller's
job - this script does not guess). direction_bearing_deg may be null: the record then
carries the factory's own undirected code, 64 ("both ways"),
instead of a guessed heading. cam_id defaults to the camera's 1-based position in the list.

TILES_DIR holds the built B*.DAT section-1 tiles (MAPAL001 naming); every file holding at
least one camera is rewritten into OUT_DIR under the same name, everything else untouched.
"""
import glob
import json
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile
import clarion_bg as cb

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


def load_cameras(path, speed_fallback):
    """cameras.json with every speed filled in. OSM rarely tags a speed on the camera node,
    and the record needs one, so speed_fallback gives it per camera id; a camera with neither
    is left out. No file means no cameras."""
    if not os.path.exists(path):
        print('no camera file at %s: the card gets no camera icons (extract_cameras.py makes one)' % path)
        return []
    cams = []
    for c in json.load(open(path)):
        if c['speed_limit_kmh'] is None:
            if c['id'] not in speed_fallback:
                print('camera %s has no speed and no camera_speed_fallback entry: left out' % c['id'])
                continue
            c['speed_limit_kmh'] = speed_fallback[c['id']]
        cams.append(c)
    return cams


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
