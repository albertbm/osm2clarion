#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Put road shapes into a coarse map file that keeps its own background.

road_strip.py leaves LEV3456R.DAT with the factory's land, water and names at levels 6 to 3
and no roads. This reads that file through lev_file and gives the parcels our roads cross a
road-shape record at levels 3 and 4 and a road block at level 5, with route numbers in the
background records' names sections (lev_roads.build). A parcel the factory had no
background for gets nothing. Section 2, the coarse graph, is left as it is.

    lev_add_roads.py --profile NAME IN.DAT OUT.DAT [roads.json]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_profile
import lev_file
import lev_roads
import rd_build


def add(src, out, P, ways):
    with open(src, 'rb') as f:
        lev = lev_file.read(f.read())
    if lev['fmt'] is not P.fmt:
        raise SystemExit('%s is %s, the profile says %s' % (src, lev['fmt'].name, P.fmt.name))
    rd_build.use_profile(P)
    levels = {L['level']: L['dirs'] for L in lev['sections'][0]['levels']}
    stats = lev_roads.build(levels, ways, P.mesh_origin, P.country, keep=True)
    b = lev_file.write(lev)
    with open(out, 'wb') as f:
        f.write(b)
    return stats, len(b)


if __name__ == '__main__':
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    rpath = argv[2] if len(argv) > 2 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    stats, size = add(argv[0], argv[1], P, json.load(open(rpath)))
    for level, (nrec, nsh, npt, nlab) in sorted(stats.items()):
        print('level %d roads: %d records, %d shapes, %d points, %d route labels' % (level, nrec, nsh, npt, nlab))
    print('wrote %s %d B' % (argv[1], size))
