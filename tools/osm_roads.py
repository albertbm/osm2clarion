#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Highway ways of an OSM extract as JSON for rd_build.py, lev_roads.py and lev_build.py. --public keeps drivable public roads, named service roads included, nothing closed to cars or tagged as an area. --relations/--box clip ways to a region, adding a point at each crossing so two ways sharing a point still share it after clipping. --ramp-names names unnamed *_link ways after the road they lead to."""
import collections, json, math, sys

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
                            oneway=w.tags.get('oneway'), junction=w.tags.get('junction'), dir=oneway(w.tags),
                            maxspeed=w.tags.get('maxspeed'), nodes=pts))
            d = dest_tags(w.tags)
            if d: out[-1]['dest'] = d
    H().apply_file(pbf, locations=True)
    inherit_dest(out)
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


DEST = ('destination', 'destination:ref', 'destination:ref:to', 'destination:street', 'junction:ref')


def dest_tags(t):
    """The signpost tags of a way, or None; rd_build --signposts turns them into section 4."""
    d = {k: t[k] for k in DEST if k in t}
    return d or None


def inherit_dest(ways):
    """Give an untagged link way the destination of the one tagged link way it feeds into,
    walking back along link-only chains up to four hops, so the sign stands where the ramp
    leaves the road rather than at a split further down. Returns how many ways gained one."""
    at_end = {}
    for w in ways:
        if w['hw'].endswith('_link'): at_end.setdefault(tuple(w['nodes'][-1]), []).append(w)
    gained = 0
    for w in [w for w in ways if w['hw'].endswith('_link') and w.get('dest')]:
        cur = w
        for _ in range(4):
            feeders = [f for f in at_end.get(tuple(cur['nodes'][0]), []) if f is not cur]
            if len(feeders) != 1 or feeders[0].get('dest') or feeders[0].get('dir', 0) < 0: break
            feeders[0]['dest'] = w['dest']; gained += 1; cur = feeders[0]
    return gained


def oneway_dir(ow, junction, hw):
    """1 one-way along the way, -1 against it, 0 both ways, from the raw tag values. The
    legacy bool form of `ow` (no junction/-1 info left) is read as oneway=yes/absent."""
    if isinstance(ow, bool): return 1 if ow else 0
    if ow in ('yes', '1', 'true'): return 1
    if ow == '-1': return -1
    if ow is not None: return 0
    if junction in ('roundabout', 'circular'): return 1
    if hw in ('motorway', 'motorway_link'): return 1
    return 0


def oneway(tags):
    """1 one-way along the way, -1 against it, 0 both ways."""
    return oneway_dir(tags.get('oneway'), tags.get('junction'), tags.get('highway'))


LINK = {'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}
CLASS_RANK = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
              'residential', 'living_street', 'service']


def _class_rank(hw):
    base = hw[:-5] if hw in LINK else hw
    return CLASS_RANK.index(base) if base in CLASS_RANK else len(CLASS_RANK)


def _reachable(way, point, away):
    """True if `point` is an end of `way` and it can be driven past that end: away from it
    (`away=True`, for leads-to) or into it from the other end (`away=False`, for trails-from).
    A two-way way always qualifies."""
    d = way.get('dir', 0)
    if tuple(way['nodes'][0]) == point: return d != (-1 if away else 1)
    if tuple(way['nodes'][-1]) == point: return d != (1 if away else -1)
    # OSM often leaves a road unsplit where a ramp joins; it runs both into and out of an interior node
    return point in map(tuple, way['nodes'])


def _chain(way, end, idx, visited, away):
    """Named roads reached by walking the chain of links from one end of `way`, either
    onward in its travel direction (`away=True`) or backward against it (`away=False`)."""
    point = tuple(way['nodes'][end])
    visited = visited | {way['id']}
    found = []
    for other in idx.get(point, ()):
        if other['id'] in visited or not _reachable(other, point, away): continue
        if other['hw'] in LINK:
            if tuple(other['nodes'][0]) == point: nxt = (-1,)
            elif tuple(other['nodes'][-1]) == point: nxt = (0,)
            else:
                d = other.get('dir', 0)
                nxt = (0, -1) if d == 0 else (-1,) if (d == 1) == away else (0,)
            for e in nxt: found.extend(_chain(other, e, idx, visited, away))
        elif other.get('name'):
            found.append((other['name'], other['hw']))
    return found


def _leads_to(way, end, idx, visited):
    """Named roads reached by walking the chain of links onward from one end of `way`."""
    return _chain(way, end, idx, visited, away=True)


def _trails_from(way, end, idx, visited):
    """Named roads that could have driven into one end of `way`, walking the link chain back."""
    return _chain(way, end, idx, visited, away=False)


def _pick(candidates):
    """Best of several leads-to candidates: higher road class first, then the most common name, then alphabetical."""
    if not candidates: return None
    best = min(_class_rank(hw) for _, hw in candidates)
    names = [n for n, hw in candidates if _class_rank(hw) == best]
    counts = collections.Counter(names)
    top = max(counts.values())
    return sorted(n for n, c in counts.items() if c == top)[0]


def ramp_name(way, idx):
    """A link way's name: its own, else the road it leads to (the oneway direction, both
    ends tried on a two-way link), else the road it trails from."""
    if way.get('name') or way['hw'] not in LINK: return way.get('name')
    d = way.get('dir', 0)
    ends = (-1,) if d == 1 else (0,) if d == -1 else (0, -1)
    name = _pick([c for e in ends for c in _leads_to(way, e, idx, set())])
    if name or d == 0: return name
    return _pick(_trails_from(way, 0 if ends[0] == -1 else -1, idx, set()))


def name_ramps(ways):
    """Set a name on every unnamed link way in `ways` that the rule above can name."""
    idx = collections.defaultdict(list)
    for w in ways:
        for p in w['nodes']: idx[tuple(p)].append(w)
    for w in ways:
        if w['hw'] in LINK and not w.get('name'):
            name = ramp_name(w, idx)
            if name: w['name'] = name


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
            ow = oneway(t); d = dest_tags(t)
            for r in runs:
                out.append(dict(id=w.id, hw=t['highway'], name=t.get('name'), ref=t.get('ref'),
                                oneway=t.get('oneway'), junction=t.get('junction'),
                                dir=ow, maxspeed=t.get('maxspeed'), nodes=[list(p) for p in r]))
                if d: out[-1]['dest'] = d
    H().apply_file(pbf, locations=True)
    why['destination inherited by a feeder link'] = inherit_dest(out)
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
        if '--ramp-names' in argv: name_ramps(ways)
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
    if '--ramp-names' in argv: name_ramps(ways)
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
