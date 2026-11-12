#!/usr/bin/env python3
"""Road set for clean-room step 2: the drivable path between two named streets.

    gen_roads_path.py iceland-latest.osm.pbf turns_iceland.json ROADS_OUT TURNS_OUT [--reach METRES]
                      [--also NAME,NAME] [--to NAME,NAME [--by distance|time]]

Start and goal street names, and the search box, come from the build config's
`roads_poc` (start, goal, box), the [roads_poc] table of the profile. Needs $O2C_CONFIG set.

Shortest drivable route (oneway respected) from any node of the start street to any
node of the goal street, over the highway ways in the config's box. Output: every way
the route runs along, all of the start street, and the goal street cut to METRES
(default 400) around the join, in osm_roads.py's format; and the turn restrictions
whose from and to ways are both in the set. --also adds every way of the named streets,
whole.

--to adds a further route from the start street to each named street: every way along
it, whole (the goal street too), and every way of that street. --by time weighs those
routes by travel time at SPEED's class speeds instead of by length.
"""
import heapq, json, math, os, sys
import osmium

_CACHE = {}


def roads_poc():
    if 'v' not in _CACHE:
        cfg_path = os.environ.get('O2C_CONFIG')
        if not cfg_path:
            raise SystemExit('gen_roads_path.py needs O2C_CONFIG set to a build config')
        _CACHE['v'] = json.load(open(os.path.expanduser(cfg_path)))['roads_poc']
    return _CACHE['v']


DRIVE = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
         'living_street', 'service', 'motorway_link', 'trunk_link', 'primary_link',
         'secondary_link', 'tertiary_link'}
NOCAR = {'no', 'private'}
# km/h per highway class for --by time; a link road runs at 80% of its class
SPEED = dict(motorway=100, trunk=80, primary=60, secondary=50, tertiary=40, unclassified=30, residential=30,
             living_street=10, service=15)


def speed(hw):
    return SPEED[hw.replace('_link', '')] * (0.8 if hw.endswith('_link') else 1)


class Ways(osmium.SimpleHandler):
    def __init__(self):
        super().__init__(); self.ways = []

    def way(self, w):
        t = w.tags
        if t.get('highway') not in DRIVE or t.get('access') in NOCAR or t.get('motor_vehicle') in NOCAR:
            return
        nodes = [(n.ref, n.lon, n.lat) for n in w.nodes if n.location.valid()]
        box = roads_poc()['box']
        if len(nodes) < 2 or not any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for _, x, y in nodes):
            return
        ow = t.get('oneway')
        ow = 1 if ow in ('yes', '1', 'true') or (t.get('junction') == 'roundabout' and ow != 'no') else -1 if ow == '-1' else 0
        self.ways.append(dict(id=w.id, hw=t['highway'], name=t.get('name'), ref=t.get('ref'), oneway=ow == 1,
                              maxspeed=t.get('maxspeed'), dir=ow, nodes=nodes))


def metres(a, b):
    m = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111320 * m, (b[1] - a[1]) * 111320)


def route(ways, start=None, goal=None, weigh='distance'):
    """(cost, [(from node, to node, way id)], {node: (lon, lat)}); cost is metres, or
    seconds with weigh='time'."""
    rp = roads_poc()
    start = start or rp['start']
    goal = goal or rp['goal']
    loc, adj = {}, {}
    for w in ways:
        ns = w['nodes']
        k = 3.6 / speed(w['hw']) if weigh == 'time' else 1.0
        for i in range(len(ns) - 1):
            (a, ax, ay), (b, bx, by) = ns[i], ns[i + 1]
            loc[a], loc[b] = (ax, ay), (bx, by); d = metres((ax, ay), (bx, by)) * k
            if w['dir'] >= 0: adj.setdefault(a, []).append((b, d, w['id']))
            if w['dir'] <= 0: adj.setdefault(b, []).append((a, d, w['id']))
    src = {n for w in ways if w['name'] == start for n, _, _ in w['nodes']}
    dst = {n for w in ways if w['name'] == goal for n, _, _ in w['nodes']}
    best = {n: 0.0 for n in src}; prev = {}; q = [(0.0, n) for n in src]; heapq.heapify(q)
    while q:
        d, n = heapq.heappop(q)
        if d > best[n]: continue
        if n in dst:
            path = []
            while n in prev: p, wid = prev[n]; path.append((p, n, wid)); n = p
            return d, path[::-1], loc
        for m, e, wid in adj.get(n, ()):
            if d + e < best.get(m, 1e18): best[m] = d + e; prev[m] = (n, wid); heapq.heappush(q, (d + e, m))
    raise SystemExit('no route from %s to %s' % (start, goal))


def main():
    start, goal = roads_poc()['start'], roads_poc()['goal']
    argv = sys.argv[1:]; reach = 400.0; also = set()
    if '--reach' in argv: i = argv.index('--reach'); reach = float(argv[i + 1]); del argv[i:i + 2]
    if '--also' in argv: i = argv.index('--also'); also = set(argv[i + 1].split(',')); del argv[i:i + 2]
    to, by = [], 'distance'
    if '--to' in argv: i = argv.index('--to'); to = argv[i + 1].split(','); del argv[i:i + 2]
    if '--by' in argv: i = argv.index('--by'); by = argv[i + 1]; del argv[i:i + 2]
    assert by in ('distance', 'time'), by
    pbf, turns_in, roads_out, turns_out = argv[:4]
    h = Ways(); h.apply_file(pbf, locations=True)
    length, path, loc = route(h.ways)
    join = loc[path[-1][1]]
    on_path = list(dict.fromkeys(wid for _, _, wid in path))
    byid = {w['id']: w for w in h.ways}
    extra = []      # (street, cost, ways in order) per --to route
    for name in to:
        cost, p2, _ = route(h.ways, start, name, by)
        extra.append((name, cost, p2, list(dict.fromkeys(wid for _, _, wid in p2))))
    whole = {wid for *_, ws in extra for wid in ws} | {w['id'] for w in h.ways if w['name'] in to}
    keep = set(on_path) | whole | {w['id'] for w in h.ways if w['name'] == start or w['name'] in also}
    out = []
    for w in h.ways:
        pts = [[x, y] for _, x, y in w['nodes']]
        if w['name'] == goal and w['id'] not in whole:
            # the stretch near the join: runs of points within reach, broken where the way leaves it
            runs, cur = [], []
            for p in pts:
                if metres(p, join) <= reach: cur.append(p)
                elif cur: runs.append(cur); cur = []
            if cur: runs.append(cur)
            runs = [r for r in runs if len(r) >= 2]
            if not runs: continue
            pts = max(runs, key=len)
        elif w['id'] not in keep:
            continue
        out.append(dict(id=w['id'], hw=w['hw'], name=w['name'], ref=w['ref'], oneway=w['oneway'],
                        dir=w['dir'], maxspeed=w['maxspeed'], nodes=pts))
    ids = {w['id'] for w in out}
    turns = [r for r in json.load(open(turns_in)) if r['from'] in ids and r['to'] in ids]
    json.dump(out, open(roads_out, 'w'), ensure_ascii=False)
    json.dump(turns, open(turns_out, 'w'))
    print('route %.0f m from %s to %s, joins at %.6f,%.6f' % (length, start, goal, *join))
    print('ways on the route, in order:')
    for wid in on_path:
        w = byid[wid]; print('  %d %s %s %s' % (wid, w['hw'], w['name'], w['ref'] or ''))
    for name, cost, p2, ws in extra:
        L = sum(metres(loc[a], loc[b]) for a, b, _ in p2)
        print('route by %s from %s to %s: %.0f m%s; ways in order:' % (by, start, name, L,
              ', %.0f s' % cost if by == 'time' else ''))
        for wid in ws:
            w = byid[wid]; print('  %d %s %s %s' % (wid, w['hw'], w['name'], w['ref'] or ''))
    print('ways written %d, turn restrictions %d' % (len(out), len(turns)))
    for w in out:
        L = sum(metres(a, b) for a, b in zip(w['nodes'], w['nodes'][1:]))
        print('  %d %-14s %-22s %-4s %3d pts %5.0f m%s' % (w['id'], w['hw'], w['name'], w['ref'] or '', len(w['nodes']), L,
                                                           ' oneway' if w['oneway'] else ''))


if __name__ == '__main__':
    main()
