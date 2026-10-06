# SPDX-License-Identifier: GPL-3.0-or-later
"""Every natural=coastline way of an OSM extract as GeoJSON, for the background builders.

Needs osmium, which nothing else in the chain does.

    ./extract_coast.py iceland-latest.osm.pbf coast_iceland.json
"""
import osmium, json, sys
class H(osmium.SimpleHandler):
    def __init__(self): super().__init__(); self.ways = []
    def way(self, w):
        if w.tags.get('natural') != 'coastline': return
        try: pts = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError: return
        if len(pts) >= 2: self.ways.append((w.id, pts))
h = H(); h.apply_file(sys.argv[1], locations=True)
fc = dict(type='FeatureCollection', features=[dict(type='Feature', properties=dict(osm_way_id=i),
          geometry=dict(type='LineString', coordinates=[list(p) for p in pts])) for i, pts in h.ways])
json.dump(fc, open(sys.argv[2], 'w'))
print('coastline ways', len(h.ways), 'points', sum(len(p) for _, p in h.ways))
