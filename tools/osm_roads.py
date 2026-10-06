#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every highway way of an OSM extract as JSON for rd_build.py and lev_roads.py.

    ./osm_roads.py iceland-latest.osm.pbf roads_iceland.json
"""
import json, sys
import osmium

KEEP = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
        'living_street', 'service', 'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}

class Roads(osmium.SimpleHandler):
    def __init__(self):
        super().__init__(); self.ways = []
    def way(self, w):
        hw = w.tags.get('highway')
        if hw not in KEEP: return
        pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(pts) < 2: return
        self.ways.append(dict(id=w.id, hw=hw, name=w.tags.get('name'), ref=w.tags.get('ref'),
                              oneway=w.tags.get('oneway') == 'yes', maxspeed=w.tags.get('maxspeed'), nodes=pts))

if __name__ == '__main__':
    h = Roads(); h.apply_file(sys.argv[1], locations=True)
    json.dump(h.ways, open(sys.argv[2], 'w')); print('ways', len(h.ways))
