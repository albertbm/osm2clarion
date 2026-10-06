"""Decode a tile's background geometry (section 1) to longitude/latitude."""
import json, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile, clarion_bg
from clarion_bg import LEVELS, parcel_geo, UNITS

def decode_tile(d):
    """Yield dicts: level, y0, x0, row, col, layer, type, closed, coords[(lon,lat)]."""
    t = clarion_tile.read_tile(d)
    sec = t['sections'][0]
    for L in sec['levels']:
        level = L['level']
        if level not in LEVELS: continue
        for D in L['dirs']:
            for row, s in D['slots'].items():
                for r in s['recs']:
                    if r['raw'][:2] != b'\x58\x85': continue
                    plain = zlib.decompress(r['raw'])
                    if struct.unpack_from('>H', plain, 4)[0] != 0x19: continue
                    col = r['k'] // 2
                    rec = clarion_bg.parse_record(plain)
                    lon0, lat0, w, h = parcel_geo(level, D['y'], D['x'], row, col)
                    for li, g in rec['layers'].items():
                        if 'raw' in g: continue
                        for sh in g['shapes']:
                            coords = [(lon0 + x / UNITS * w, lat0 + y / UNITS * h) for x, y in sh['pts']]
                            yield dict(level=level, y0=D['y'], x0=D['x'], row=row, col=col, layer=li,
                                       type=sh['type'], kind=g['kind'], coords=coords)

def to_geojson(feats):
    out = []
    for f in feats:
        if f['kind'] == 0: geom = dict(type='Point', coordinates=list(f['coords'][0]))
        elif f['kind'] & 0x80: geom = dict(type='Polygon', coordinates=[[list(c) for c in f['coords']]])
        else: geom = dict(type='LineString', coordinates=[list(c) for c in f['coords']])
        props = {k: v for k, v in f.items() if k != 'coords'}
        props['type'] = '0x%04x' % props['type']
        out.append(dict(type='Feature', geometry=geom, properties=props))
    return dict(type='FeatureCollection', features=out)

if __name__ == '__main__':
    d = open(sys.argv[1], 'rb').read()
    gj = to_geojson(decode_tile(d))
    json.dump(gj, open(sys.argv[2], 'w'))
    print('features', len(gj['features']))
