#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Highway ways of an OSM extract as JSON for rd_build.py, lev_roads.py and lev_build.py.

--public keeps drivable public roads only, named service roads included, nothing closed to
cars or tagged as an area. --relations or --box clips ways to a region, adding a point where
a way crosses the boundary so two ways sharing a point still share it after clipping.
"""
import json, math, sys

KEEP = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
        'living_street', 'service', 'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}


def all_ways(pbf):
    import osmium
    out = []
    class H(osmium.SimpleHandler):
        def way(self, w):
            hw = w.tags.get('highway')
            if hw not in KEEP: return
            pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
            if len(pts) < 2: return
            out.append(dict(id=w.id, hw=hw, name=w.tags.get('name'), ref=w.tags.get('ref'),
                            oneway=w.tags.get('oneway') == 'yes', dir=oneway(w.tags),
                            maxspeed=w.tags.get('maxspeed'), nodes=pts))
    H().apply_file(pbf, locations=True)
    return out


DRIVE = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
         'living_street', 'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}
SERVICE_NOT = {'parking_aisle', 'driveway', 'drive-through', 'parking'}
NOCAR = {'no', 'private'}


def keep(tags):
    """Why a way is left out, or None to keep it."""
    hw = tags.get('highway')
    if hw == 'service':
        if not tags.get('name'): return 'service, unnamed'
        if tags.get('service') in SERVICE_NOT: return 'service=' + tags['service']
    elif hw not in DRIVE:
        return 'not a road class'
    if tags.get('access') in NOCAR or tags.get('motor_vehicle') in NOCAR or tags.get('motorcar') in NOCAR:
        return 'closed to cars'
    if tags.get('area') == 'yes': return 'area'
    return None


def oneway(tags):
    """1 one-way along the way, -1 against it, 0 both ways."""
    ow = tags.get('oneway')
    if ow in ('yes', '1', 'true'): return 1
    if ow == '-1': return -1
    if ow is not None: return 0
    if tags.get('junction') in ('roundabout', 'circular'): return 1
    if tags.get('highway') in ('motorway', 'motorway_link'): return 1
    return 0


def clip(pts, inside, cross):
    """Inside runs of a polyline; cross(a, b) gives the crossing point of segment a-b."""
    runs, cur = [], []
    for i, p in enumerate(pts):
        ins = inside(p)
        if i and ins != inside(pts[i - 1]):
            x = cross(pts[i - 1], p)
            if ins: cur = [x]
            else: cur.append(x); runs.append(cur); cur = []
        if ins: cur.append(p)
    if cur: runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def region(pbf, relations=None, box=None, buffer_m=0.0):
    import shapely
    from shapely.ops import unary_union
    if box:
        g = shapely.box(*box)
    else:
        import osmium, shapely.wkb
        fab = osmium.geom.WKBFactory(); found = {}
        class H(osmium.SimpleHandler):
            def area(self, a):
                if not a.from_way() and a.orig_id() in relations:
                    found[a.orig_id()] = (a.tags.get('name'), shapely.wkb.loads(fab.create_multipolygon(a), hex=True))
        H().apply_file(pbf, locations=True)
        missing = set(relations) - set(found)
        if missing: raise SystemExit('relations not found as areas: %s' % sorted(missing))
        g = unary_union([v[1] for v in found.values()])
        region.names = {k: v[0] for k, v in found.items()}
    if buffer_m:
        lat = (g.bounds[1] + g.bounds[3]) / 2
        g = g.buffer(buffer_m / (111320 * math.cos(math.radians(lat))), 4)
    return g


def predicates(g):
    import shapely
    from shapely.geometry import LineString, Point
    shapely.prepare(g); edge = g.boundary
    def inside(p): return bool(shapely.contains_xy(g, p[0], p[1]))
    def cross(a, b):
        x = LineString([a, b]).intersection(edge)
        pts = [x] if x.geom_type == 'Point' else list(getattr(x, 'geoms', []))
        pts = [q for q in pts if q.geom_type == 'Point'] or [Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)]
        q = min(pts, key=lambda q: (q.x - a[0]) ** 2 + (q.y - a[1]) ** 2)
        return (q.x, q.y)
    return inside, cross


def select(pbf, inside, cross, public=True):
    import collections, osmium
    out, why = [], collections.Counter()
    class H(osmium.SimpleHandler):
        def way(self, w):
            t = w.tags
            if 'highway' not in t: return
            r = keep(t) if public else (None if t['highway'] in KEEP else 'not a road class')
            if r: why[r] += 1; return
            pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
            runs = clip(pts, inside, cross)
            if not runs: why['outside'] += 1; return
            if len(runs) > 1 or len(runs[0]) != len(pts): why['clipped at the boundary'] += 1
            ow = oneway(t)
            for r in runs:
                out.append(dict(id=w.id, hw=t['highway'], name=t.get('name'), ref=t.get('ref'), oneway=ow == 1,
                                dir=ow, maxspeed=t.get('maxspeed'), nodes=[list(p) for p in r]))
    H().apply_file(pbf, locations=True)
    return out, why


def main(argv):
    pbf, dest = argv[0], argv[1]
    rel = box = None; buf = 0.0; turns = None
    if '--relations' in argv: rel = {int(v) for v in argv[argv.index('--relations') + 1].split(',')}
    if '--box' in argv: box = [float(v) for v in argv[argv.index('--box') + 1].split(',')]
    if '--buffer' in argv: buf = float(argv[argv.index('--buffer') + 1])
    if '--turns' in argv: i = argv.index('--turns'); turns = argv[i + 1], argv[i + 2]
    if '--public' not in argv and not (rel or box):
        ways = all_ways(pbf)
        json.dump(ways, open(dest, 'w')); print('ways', len(ways))
        return
    if rel or box:
        g = region(pbf, rel, box, buf)
        inside, cross = predicates(g)
        print('region bounds %.4f %.4f %.4f %.4f' % g.bounds)
        for k, v in sorted(getattr(region, 'names', {}).items()): print('  relation %d %s' % (k, v))
    else:
        inside, cross = (lambda p: True), None
    ways, why = select(pbf, inside, cross, public='--public' in argv)
    json.dump(ways, open(dest, 'w'), ensure_ascii=False)
    print('ways %d (%d pieces)' % (len({w['id'] for w in ways}), len(ways)))
    for k, v in why.most_common(): print('  %-26s %d' % (k, v))
    if turns:
        ids = {w['id'] for w in ways}
        t = [r for r in json.load(open(turns[0])) if r['from'] in ids and r['to'] in ids]
        json.dump(t, open(turns[1], 'w'))
        print('turn restrictions %d' % len(t))


if __name__ == '__main__':
    main(sys.argv[1:])
