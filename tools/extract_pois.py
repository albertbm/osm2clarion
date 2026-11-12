#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Facilities of an OSM extract as JSON, for bg_tiles.py --pois and poi_point.py.

Four genres of the unit's search list, with the ICODE codes the factory card uses
: restaurant 6 with a cuisine sub-code, hotel 5 with a chain
sub-code, hospital 11 and rest area 30 without one. A way or relation takes its centroid.
Needs osmium and shapely.

    extract_pois.py iceland-latest.osm.pbf pois.json [--rest-area-name ÁNINGARSTAÐUR] [-v]

A facility without a name is dropped, since a POINT record needs one; --rest-area-name
gives unnamed rest areas that word (plus the road ref when tagged) instead. A node and an
area of one category within 50 m whose names match are one facility (dedupe); -v lists
every pair dropped.
"""
import json
import math
import sys

RESTAURANT, HOTEL, HOSPITAL, REST_AREA = 6, 5, 11, 30
NO_SUB = 0xffff
CUISINE_OTHER, FAST_FOOD, CHAIN_OTHERS = 924, 906, 3500

# OSM cuisine= -> RESTAURANT sub-list code (REFER001/ICODE001.DAT, English list); the rest is 924 OTHER
CUISINES = {
    'african': 901, 'moroccan': 901, 'ethiopian': 901,
    'american': 902, 'grill': 902, 'steak_house': 902, 'steak': 902, 'bbq': 902, 'barbecue': 902,
    'austrian': 903,
    'chinese': 905,
    'burger': 906, 'sandwich': 906, 'hot_dog': 906, 'chicken': 906, 'kebab': 906, 'fish_and_chips': 906,
    'french': 907,
    'german': 908, 'bavarian': 908,
    'greek': 909,
    'italian': 910, 'pizza': 910,
    'japanese': 911, 'sushi': 911, 'ramen': 911,
    'indian': 912, 'nepalese': 912, 'pakistani': 912,
    'international': 913,
    'mexican': 914, 'tex-mex': 914, 'latin_american': 914, 'brazilian': 914, 'peruvian': 914, 'argentinian': 914,
    'seafood': 915, 'fish': 915,
    'spanish': 916, 'tapas': 916,
    'swiss': 917,
    'turkish': 918,
    'vegetarian': 919, 'vegan': 919,
    'vietnamese': 920, 'thai': 920,
    'asian': 921, 'korean': 921, 'indonesian': 921, 'malaysian': 921, 'filipino': 921,
    'polish': 922, 'hungarian': 922, 'czech': 922, 'balkan': 922, 'ukrainian': 922,
    'russian': 938,
}

# OSM brand= (folded to upper case) -> HOTEL sub-list code; anything else is 3500 OTHERS
CHAINS = {
    'BEST WESTERN': 3501, 'AC': 3502, 'COURTYARD': 3504, 'CROWNE PLAZA': 3505, 'HILTON': 3511,
    'HOLIDAY INN': 3512, 'INTERCONTINENTAL': 3515, 'B&B': 3516, 'B & B': 3516, 'MARRIOTT': 3517,
    'QUALITY INN': 3519, 'RADISSON': 3520, 'RADISSON BLU': 3520, 'RAMADA': 3521, 'CARLTON': 3524,
    'SHERATON': 3526, 'TRAVELODGE': 3528, 'COMFORT INN': 3537, 'IBIS': 3550, 'MERCURE': 3570,
    'NOVOTEL': 3578, 'PREMIER INN': 3583, 'SCANDIC': 3597, 'SOFITEL': 3601, 'CLARION HOTEL': 3620,
    'HYATT': 3619, 'PARK INN': 3625, 'PARK PLAZA': 3628, 'FIRST HOTELS': 3540, 'FIRST HOTEL': 3540,
}


def classify(tags):
    """(category, sub-category) of a tagged object, or None."""
    a, t = tags.get('amenity'), tags.get('tourism')
    if a == 'restaurant' or a == 'cafe':
        return RESTAURANT, cuisine_code(tags.get('cuisine'))
    if a == 'fast_food':
        return RESTAURANT, FAST_FOOD
    if t in ('hotel', 'guest_house', 'hostel', 'motel'):
        return HOTEL, chain_code(tags.get('brand'))
    if a == 'hospital' or tags.get('healthcare') == 'hospital':
        return HOSPITAL, NO_SUB
    if tags.get('highway') in ('rest_area', 'services'):
        return REST_AREA, NO_SUB
    return None


def cuisine_code(cuisine):
    for c in (cuisine or '').lower().replace(',', ';').split(';'):
        if c.strip() in CUISINES:
            return CUISINES[c.strip()]
    return CUISINE_OTHER


def chain_code(brand):
    b = (brand or '').upper().strip()
    return CHAINS.get(b) or next((code for name, code in CHAINS.items() if b.startswith(name + ' ')), CHAIN_OTHERS)


DUP_M = 50     # two objects of one category this close with matching names are one facility
CLEAN = {'\u2018': "'", '\u2019': "'", '\u00b4': "'", '`': "'", '\u201c': '"', '\u201d': '"', '\u00d7': 'x',
         '\xa0': ' ', '\u2013': '-', '\u2014': '-', '\u200b': ''}   # typographic characters the keyboard cannot reach


def clean_name(name):
    return ' '.join(''.join(CLEAN.get(c, c) for c in name).split())


def same_site(a, b):
    """Same category, within DUP_M, and the same name or one name inside the other."""
    if a['cat'] != b['cat']:
        return False
    dy = (a['lat'] - b['lat']) * 111195
    dx = (a['lon'] - b['lon']) * 111195 * math.cos(math.radians(a['lat']))
    if dx * dx + dy * dy > DUP_M * DUP_M:
        return False
    x, y = a['name'].casefold(), b['name'].casefold()
    return x in y or y in x


def dedupe(pois):
    """(kept, dropped): OSM maps many facilities twice, as a node and as the building or
    grounds around it, sometimes under a longer name. The node wins over an area centroid,
    an earlier object over a later one."""
    rank = {'n': 0, 'w': 1, 'r': 2}
    cells = {}
    kept, dropped = [], []
    for p in sorted(pois, key=lambda p: (rank[p['id'][0]], p['id'])):
        cy, cx = int(p['lat'] * 1000), int(p['lon'] * 1000)
        near = (q for dy in (-1, 0, 1) for dx in (-1, 0, 1) for q in cells.get((cy + dy, cx + dx), ()))
        twin = next((q for q in near if same_site(p, q)), None)
        if twin is None:
            kept.append(p)
            cells.setdefault((cy, cx), []).append(p)
        else:
            dropped.append((p, twin))
    return kept, dropped


def main(argv):
    import osmium
    from shapely import wkb
    verbose = '-v' in argv
    if verbose:
        argv.remove('-v')
    rest_name = None
    if '--rest-area-name' in argv:
        i = argv.index('--rest-area-name')
        rest_name = argv[i + 1]
        del argv[i:i + 2]
    src, dest = argv
    fab = osmium.geom.WKBFactory()
    out, dropped = [], {}

    def take(kind, oid, tags, lon, lat):
        cs = classify(tags)
        if cs is None:
            return
        name = tags.get('name')
        if not name:
            if cs[0] == REST_AREA and rest_name:
                name = rest_name + (' ' + tags['ref'] if tags.get('ref') else '')
            else:
                dropped[cs[0]] = dropped.get(cs[0], 0) + 1
                return
        out.append(dict(id='%s%d' % (kind, oid), name=clean_name(name), cat=cs[0], sub=cs[1],
                        lat=round(lat, 7), lon=round(lon, 7)))

    class H(osmium.SimpleHandler):
        def node(self, n):
            if n.location.valid():
                take('n', n.id, {t.k: t.v for t in n.tags}, n.location.lon, n.location.lat)

        def area(self, a):
            tags = {t.k: t.v for t in a.tags}
            if classify(tags) is None:
                return
            try:
                g = wkb.loads(bytes.fromhex(fab.create_multipolygon(a)))
            except (RuntimeError, osmium.InvalidLocationError):
                return
            c = g.centroid
            take('w' if a.from_way() else 'r', a.orig_id(), tags, c.x, c.y)

    H().apply_file(src, locations=True)
    out, twins = dedupe(out)
    out.sort(key=lambda p: (p['cat'], p['lat'], p['lon'], p['id']))
    with open(dest, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    per = {}
    for p in out:
        per[p['cat']] = per.get(p['cat'], 0) + 1
    print('pois %d: %s; unnamed dropped %s; same-site twins dropped %d'
          % (len(out), ' '.join('cat %d: %d' % kv for kv in sorted(per.items())),
             ' '.join('cat %d: %d' % kv for kv in sorted(dropped.items())) or 'none', len(twins)))
    for p, q in twins if verbose else twins[:20]:
        print('  %s %r -> %s %r' % (p['id'], p['name'], q['id'], q['name']))


if __name__ == '__main__':
    main(sys.argv[1:])
