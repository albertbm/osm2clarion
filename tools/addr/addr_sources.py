# SPDX-License-Identifier: GPL-3.0-or-later
"""Address readers for the formats other than Staðfangaskrá, as the dicts addr_csv yields,
and the profile setting that picks one.

osm: addr:street and addr:housenumber (addr:postcode, addr:city where present) on nodes,
closed building ways and multipolygon relations of a pbf. A building's point is the mean of
its first outer ring. The municipality is not looked up, so svf is empty.

ryhti: a CSV pulled from the Ryhti open_address service (columns in docs/ADDRESS-IMPORT.md).
Address data: Syke, Ryhti, CC BY 4.0, https://www.syke.fi/fi/ymparistotieto/kayttolupa-ja-vastuut
"""
import csv
import re
import sys

_NUM = re.compile(r'^\s*(\d+)\s*([^\W\d_])?\s*(?:[-–/].*)?$')

REGISTERS = ('stadfangaskra', 'ryhti')     # the CSV layouts a register file can have


def source(cfg, default=None):
    """('osm', None) or ('register', layout) from an [addr] table.

    `source = "register"` takes `register = LAYOUT`, the file being [data] addresses;
    `source = "osm"` reads the pbf. The older `format = "osm" | LAYOUT` still reads.
    `default` is the old-style format to assume when the table sets neither."""
    if 'source' in cfg and 'format' in cfg:
        raise SystemExit('[addr] sets both source and format; keep source (and register), drop format')
    if 'source' not in cfg:
        fmt = cfg.get('format', default)
        if fmt is None:
            raise SystemExit('[addr] sets no source ("register" or "osm")')
        cfg = dict(source='osm') if fmt == 'osm' else dict(source='register', register=fmt)
    if cfg['source'] == 'osm':
        return 'osm', None
    if cfg['source'] != 'register':
        raise SystemExit('[addr] source %r: use "register" or "osm"' % cfg['source'])
    if cfg.get('register') not in REGISTERS:
        raise SystemExit('[addr] register %r: one of %s' % (cfg.get('register'), ', '.join(REGISTERS)))
    return 'register', cfg['register']


def layout(cfg, default=None):
    """'osm' or the register's CSV layout: the one name a reader switches on."""
    kind, reg = source(cfg, default)
    return reg or kind


def split_housenumber(text):
    """'12a', '12 A' -> ('12', 'A'); '12-14' -> ('12', ''); None for a value with no leading number."""
    m = _NUM.match(text or '')
    if not m:
        return None
    return m.group(1), (m.group(2) or '').upper()


def _row(street, num, letter, post, lat, lon, svf='', town='', **more):
    return dict(street=street, dative='', svf=svf, num=num, letter=letter,
                post=int(post) if str(post or '').strip().isdigit() else 0,
                lat=lat, lon=lon, town=town, **more)


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
            # interned: the whole country is 3.9 million rows sharing a few hundred thousand names
            yield _row(sys.intern(street), num, (r['letter'] or '').strip().upper(), r['postcode'],
                       float(r['lat']), float(r['lon']), sys.intern(r['municipality']),
                       sys.intern((r['town'] or '').strip()), street_swe=sys.intern((r['street_swe'] or '').strip()))


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


def osm_municipalities(pbf, admin_level='8'):
    """{ref: [name, name:fi, name:sv]} of the administrative boundary relations at one level
    that carry a ref; in Finland the ref is the municipality number."""
    import osmium
    out = {}
    fp = osmium.FileProcessor(pbf, osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter('admin_level'))
    for o in fp:
        t = o.tags
        if t.get('boundary') == 'administrative' and t.get('admin_level') == admin_level and (t.get('ref') or '').isdigit():
            out.setdefault(int(t['ref']), []).extend(t.get(k) for k in ('name', 'name:fi', 'name:sv') if t.get(k))
    return out
