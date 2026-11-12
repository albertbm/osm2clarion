#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Speed and red-light cameras from OSM tags, as GeoJSON-free cameras.json for
camera_write.py: highway=speed_camera nodes and enforcement=* relations, merged - a
relation's own device node is the same physical camera as a standalone tagged node, not a
second one. Every device of a relation is a camera, so an average-speed section gives one
at each end. Direction comes from the node's own `direction` tag in degrees, or failing
that from the relation: from-node -> device, or device -> to-node when the from-node is the
device itself. Relations that put the same heading on one device give one camera; opposite
headings give one camera each way. A relation whose from and to are one point gives no
heading.

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

    used_devices = set()
    cams = []
    on_device = {}            # device node id -> its cameras, one per heading
    for rid, tags, members in relations:
        by_role = {}
        for ref, role in members:
            by_role.setdefault(role, []).append(ref)
        devices = [d for d in by_role.get('device', []) if d in nodes] \
            or [d for d in by_role.get('camera', []) if d in nodes][:1]
        used_devices.update(devices)
        frm = next((f for f in by_role.get('from', []) if f in nodes), None)
        to = next((t for t in by_role.get('to', []) if t in nodes), None)
        pos = lambda n: nodes[n][:2]
        flat_section = frm is not None and to is not None and pos(frm) == pos(to)
        for device in devices:
            lon, lat, dtags = nodes[device]
            if not in_boxes(lon, lat, boxes):
                continue
            dirn = None
            if flat_section:
                pass
            elif frm is not None and pos(frm) != (lon, lat):
                dirn = bearing(*pos(frm), lon, lat)
            elif to is not None and pos(to) != (lon, lat):
                dirn = bearing(lon, lat, *pos(to))
            same = next((c for c in on_device.get(device, []) if _same_way(c['direction_bearing_deg'], dirn)), None)
            if same is not None:
                if same['direction_bearing_deg'] != dirn:
                    same['direction_bearing_deg'] = None
                same['osm_source'].setdefault('also_relations', []).append(rid)
                continue
            cid = 'rel%d' % rid if len(devices) == 1 else 'rel%d-n%d' % (rid, device)
            cam = dict(id=cid, lat=lat, lon=lon, type=tags.get('enforcement'),
                       direction_bearing_deg=dirn, speed_limit_kmh=_kmh(tags.get('maxspeed') or dtags.get('maxspeed')),
                       osm_source=dict(relation=rid, device_node=device, from_node=frm, to_node=to, tags=tags))
            on_device.setdefault(device, []).append(cam)
            cams.append(cam)

    for nid, (lon, lat, tags) in nodes.items():
        if tags.get('highway') != 'speed_camera' or nid in used_devices:
            continue
        dirn = float(tags['direction']) if 'direction' in tags else None
        cams.append(dict(id='node%d' % nid, lat=lat, lon=lon, type='speed',
                          direction_bearing_deg=dirn, speed_limit_kmh=_kmh(tags.get('maxspeed')),
                          osm_source=dict(node=nid, tags=tags)))

    with open(dest, 'w') as f:
        json.dump(cams, f, indent=1, ensure_ascii=False)
    print('cameras', len(cams), 'with direction', sum(1 for c in cams if c['direction_bearing_deg'] is not None),
          'with speed limit', sum(1 for c in cams if c['speed_limit_kmh'] is not None))


def _same_way(a, b):
    """No heading merges with any heading; two headings merge within 45 degrees."""
    if a is None or b is None:
        return True
    return abs((a - b + 180) % 360 - 180) < 45


def _kmh(v):
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


if __name__ == '__main__':
    main(sys.argv[1:])
