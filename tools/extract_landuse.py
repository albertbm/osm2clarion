#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Land-use and water polygons of an OSM extract as GeoJSON, for bg_tiles.py --landuse.

The tag rules follow Geofabrik's Shortbread schema (land and water_polygons layers), cut
down to the kinds clarion_bg.AREA_CLASSES can draw. Each feature carries its kind; holes are
kept in the GeoJSON, though the encoder draws outer rings only.

    ./extract_landuse.py EXTRACT.osm.pbf OUT.json [--bbox lon0,lat0,lon1,lat1] [--kinds forest,water]

--bbox clips every polygon to the box, for a build that should carry one area only.
--kinds keeps only the named kinds.
Needs osmium and shapely.
"""
import json
import sys

# (key, values, kind), first match wins, as in Shortbread's land layer
RULES = [
    ('natural', {'water'}, 'water'),
    ('landuse', {'reservoir', 'basin'}, 'water'),
    ('waterway', {'riverbank', 'dock', 'canal'}, 'river'),
    ('landuse', {'forest'}, 'forest'),
    ('natural', {'wood'}, 'forest'),
    ('natural', {'heath', 'scrub'}, 'heath'),
    ('leisure', {'park', 'garden', 'golf_course'}, 'park'),
    ('landuse', {'grass', 'meadow', 'village_green', 'recreation_ground', 'cemetery'}, 'park'),
]


def classify(tags):
    """Shortbread-style kind for a tag dict, or None. water=river|canal makes a river."""
    for key, values, kind in RULES:
        if tags.get(key) in values:
            if kind == 'water' and tags.get('water') in ('river', 'canal', 'stream'):
                return 'river'
            return kind
    return None


def main(argv):
    import osmium
    from shapely import wkb
    from shapely.geometry import box, mapping
    clip = None
    if '--bbox' in argv:
        i = argv.index('--bbox')
        clip = box(*(float(v) for v in argv[i + 1].split(',')))
        del argv[i:i + 2]
    kinds = None
    if '--kinds' in argv:
        i = argv.index('--kinds')
        kinds = set(argv[i + 1].split(','))
        del argv[i:i + 2]
    src, dest = argv
    fab = osmium.geom.WKBFactory()
    feats = []

    class H(osmium.SimpleHandler):
        def area(self, a):
            kind = classify({t.k: t.v for t in a.tags})
            if kind is None or (kinds and kind not in kinds):
                return
            try:
                g = wkb.loads(fab.create_multipolygon(a), hex=True)
            except (RuntimeError, osmium.InvalidLocationError):
                return
            if clip is not None:
                if not g.intersects(clip):
                    return
                g = g.intersection(clip)
            if g.is_empty or g.area <= 0:
                return
            osm = ('way/%d' if a.from_way() else 'relation/%d') % a.orig_id()
            feats.append({'type': 'Feature', 'properties': {'kind': kind, 'osm': osm, 'name': a.tags.get('name')},
                          'geometry': mapping(g)})

    H().apply_file(src, locations=True)
    with open(dest, 'w') as f:
        json.dump({'type': 'FeatureCollection', 'features': feats}, f, ensure_ascii=False)
    count = {}
    for ft in feats:
        count[ft['properties']['kind']] = count.get(ft['properties']['kind'], 0) + 1
    print('land-use polygons', len(feats), dict(sorted(count.items())))


if __name__ == '__main__':
    main(sys.argv[1:])
