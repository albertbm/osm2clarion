#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Speed and red-light cameras from OSM tags, as GeoJSON-free cameras.json for
camera_write.py: highway=speed_camera nodes and enforcement=* relations, merged - a
relation's own device node is the same physical camera as a standalone tagged node, not a
second one. Direction comes from the node's own `direction` tag in degrees, or failing
that from the relation's from-node -> device-node vector; neither gives a guess.

    extract_cameras.py PBF OUT.json [--bbox lon0,lat0,lon1,lat1 ...]

--bbox may repeat; a node (or a relation's device node) outside every box is dropped.
Needs osmium.
"""
import json
import math
import sys


def in_boxes(lon, lat, boxes):
    return not boxes or any(x0 <= lon <= x1 and y0 <= lat <= y1 for x0, y0, x1, y1 in boxes)


def bearing(lon0, lat0, lon1, lat1):
    """Flat-earth bearing, fine at the scale of one junction: 0 = north, clockwise."""
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    return math.degrees(math.atan2((lon1 - lon0) * kx, lat1 - lat0)) % 360


def main(argv):
    import osmium
    boxes = []
    while '--bbox' in argv:
        i = argv.index('--bbox')
        boxes.append(tuple(float(v) for v in argv[i + 1].split(',')))
        del argv[i:i + 2]
    src, dest = argv

    nodes = {}       # id -> (lon, lat, tags)
    relations = []   # (id, tags, [(ref, role)])

    class H(osmium.SimpleHandler):
        def node(self, n):
            lon, lat = n.location.lon, n.location.lat
            if in_boxes(lon, lat, boxes):
                nodes[n.id] = (lon, lat, {t.k: t.v for t in n.tags})

        def relation(self, r):
            if 'enforcement' not in r.tags:
                return
            relations.append((r.id, dict(r.tags), [(m.ref, m.role) for m in r.members if m.type == 'n']))

    H().apply_file(src, locations=True)

    device_of_relation = {}   # relation id -> device node id
    cams = []
    for rid, tags, members in relations:
        by_role = {}
        for ref, role in members:
            by_role.setdefault(role, []).append(ref)
        device = next((d for d in by_role.get('device', []) if d in nodes), None) \
            or next((d for d in by_role.get('camera', []) if d in nodes), None)
        if device is None:
            continue
        device_of_relation[rid] = device
        lon, lat, dtags = nodes[device]
        if not in_boxes(lon, lat, boxes):
            continue
        dirn = None
        frm = next((f for f in by_role.get('from', []) if f in nodes), None)
        if frm is not None:
            flon, flat = nodes[frm][:2]
            dirn = bearing(flon, flat, lon, lat)
        cams.append(dict(id='rel%d' % rid, lat=lat, lon=lon, type=tags.get('enforcement'),
                          direction_bearing_deg=dirn, speed_limit_kmh=_kmh(tags.get('maxspeed') or dtags.get('maxspeed')),
                          osm_source=dict(relation=rid, device_node=device, from_node=frm, tags=tags)))

    used_devices = set(device_of_relation.values())
    for nid, (lon, lat, tags) in nodes.items():
        if tags.get('highway') != 'speed_camera' or nid in used_devices:
            continue
        dirn = float(tags['direction']) if 'direction' in tags else None
        cams.append(dict(id='node%d' % nid, lat=lat, lon=lon, type='speed',
                          direction_bearing_deg=dirn, speed_limit_kmh=_kmh(tags.get('maxspeed')),
                          osm_source=dict(node=nid, tags=tags)))

    json.dump(cams, open(dest, 'w'), indent=1, ensure_ascii=False)
    print('cameras', len(cams), 'with direction', sum(1 for c in cams if c['direction_bearing_deg'] is not None),
          'with speed limit', sum(1 for c in cams if c['speed_limit_kmh'] is not None))


def _kmh(v):
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


if __name__ == '__main__':
    main(sys.argv[1:])
