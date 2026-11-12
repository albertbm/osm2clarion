#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Put road shapes into a coarse map file that keeps its own background.

road_strip.py leaves LEV3456R.DAT with the factory's land, water and names at levels 6 to 3
and no roads. This reads that file through lev_file and gives the parcels our roads cross a
road-shape record at levels 3 and 4 and a road block at level 5, with route numbers in the
background records' names sections (lev_roads.build). A parcel the factory had no
background for gets nothing.

With --l2-tile the coarse graph goes in as well: lev_graph.build makes the level-4 and
level-5 graph records from the level-2 graph of the tiles named, as lev_build.py does for a
clean-room file, and writes the hierarchy flags into those tiles.

    lev_add_roads.py --profile NAME [--l2-tile TILE ...] IN.DAT OUT.DAT [roads.json]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_profile
import lev_file
import lev_roads
import rd_build


def add(src, out, P, ways, l2_tiles=()):
    with open(src, 'rb') as f:
        lev = lev_file.read(f.read())
    if lev['fmt'] is not P.fmt:
        raise SystemExit('%s is %s, the profile says %s' % (src, lev['fmt'].name, P.fmt.name))
    rd_build.use_profile(P)
    levels = {L['level']: L['dirs'] for L in lev['sections'][0]['levels']}
    stats = lev_roads.build(levels, ways, P.mesh_origin, P.country, keep=True,
                            only=(3,) if l2_tiles else (3, 4, 5))
    if l2_tiles:
        import lev_build
        import lev_graph
        dirs, gstats = lev_graph.build(ways, P, list(l2_tiles))
        # the route line is drawn from the shape of each graph link, so both come from one graph
        stats.update({k: v[:4] for k, v in lev_roads.graph_build(levels, gstats['shapes'], P.country, keep=True).items()})
        for L in lev['sections'][1]['levels']:
            if L['level'] in dirs: L['dirs'] = [dirs[L['level']]]
        cells = lev_build.arcmin(*lev_build.cells_box(P.cells))
        fields = dict(lev['fields'])
        fields['box_rows'] = [(sid, r) for sid, r in fields['box_rows'] if sid != 2] + [
            (2, [(5, 0, tuple(P.bounds)), (4, 0, cells)])]
        lev['fields'] = fields
        stats.update({'graph %d' % k: gstats[k] for k in (4, 5)})
    b = lev_file.write(lev)
    with open(out, 'wb') as f:
        f.write(b)
    return stats, len(b)


if __name__ == '__main__':
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    l2_tiles = []
    while '--l2-tile' in argv:
        i = argv.index('--l2-tile'); l2_tiles.append(argv[i + 1]); del argv[i:i + 2]
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    rpath = argv[2] if len(argv) > 2 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    stats, size = add(argv[0], argv[1], P, json.load(open(rpath)), l2_tiles)
    for level in (3, 4, 5):
        print('level %d roads: %d records, %d shapes, %d points, %d route labels' % ((level,) + tuple(stats[level])))
    for level in (4, 5):
        if 'graph %d' % level in stats:
            print('level %d graph: %d records, %d nodes, %d links' % ((level,) + tuple(stats['graph %d' % level])))
    print('wrote %s %d B' % (argv[1], size))
