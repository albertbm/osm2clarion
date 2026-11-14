# SPDX-License-Identifier: GPL-3.0-or-later
"""Address readers for the formats other than Staðfangaskrá, as the dicts addr_csv yields.

osm: addr:street and addr:housenumber (addr:postcode, addr:city where present) on nodes,
closed building ways and multipolygon relations of a pbf. A building's point is the mean of
its first outer ring. The municipality is not looked up, so svf is empty.

ryhti: a CSV pulled from the Ryhti open_address service (columns in docs/ADDRESS-IMPORT.md).
Address data: Syke, Ryhti, CC BY 4.0, https://www.syke.fi/fi/ymparistotieto/kayttolupa-ja-vastuut
"""
import csv
import re

_NUM = re.compile(r'^\s*(\d+)\s*([^\W\d_])?\s*(?:[-–/].*)?$')


def split_housenumber(text):
    """'12a', '12 A' -> ('12', 'A'); '12-14' -> ('12', ''); None for a value with no leading number."""
    m = _NUM.match(text or '')
    if not m:
        return None
    return m.group(1), (m.group(2) or '').upper()


def _row(street, num, letter, post, lat, lon, svf='', town=''):
    return dict(street=street, dative='', svf=svf, num=num, letter=letter,
                post=int(post) if str(post or '').strip().isdigit() else 0,
                lat=lat, lon=lon, town=town)


def _inside(lat, lon, box):
    lo_lat, hi_lat, lo_lon, hi_lon = box
    return lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon


def ryhti_addresses(path, box=None, municipalities=None):
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            street = (r['street'] or '').strip()
            num = (r['number'] or '').strip()
            if not street or not num.isdigit():
                continue
            if municipalities is not None:
                if r['municipality'] not in municipalities:
                    continue
            elif box is not None and not _inside(float(r['lat']), float(r['lon']), box):
                continue
            yield _row(street, num, (r['letter'] or '').strip().upper(), r['postcode'],
                       float(r['lat']), float(r['lon']), r['municipality'], (r['town'] or '').strip())


def _osm_row(tags, lat, lon, box):
    street = (tags.get('addr:street') or '').strip()
    hn = split_housenumber(tags.get('addr:housenumber'))
    if not street or hn is None or not _inside(lat, lon, box):
        return None
    return _row(street, hn[0], hn[1], (tags.get('addr:postcode') or '').strip(), lat, lon,
                '', (tags.get('addr:city') or '').strip())


def osm_addresses(pbf, box):
    import osmium
    key = osmium.filter.KeyFilter('addr:housenumber')
    fp = osmium.FileProcessor(pbf).with_areas(key).with_filter(key)
    for o in fp:
        if o.is_node():
            if not o.location.valid():
                continue
            lat, lon = o.location.lat, o.location.lon
        elif o.is_area():
            ring = next(iter(o.outer_rings()), None)
            pts = [(n.lat, n.lon) for n in ring] if ring is not None else []
            if not pts:
                continue
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
        else:
            continue
        row = _osm_row(o.tags, lat, lon, box)
        if row:
            yield row
