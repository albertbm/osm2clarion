"""Read an address CSV and its postcode register, filtered to a box or a municipality set.

The column names and the box come from config['addr'] - this module reads the Icelandic
Stadfangaskra format (config['addr']['format'] == 'stadfangaskra': columns N_HNIT_WGS84,
E_HNIT_WGS84, HEITI_NF, HEITI_TGF, SVFNR, HUSNR, BOKST, POSTNR; a ';'-delimited postcode
register), since that is the only format measured so far. A different country's register
would need its own column mapping here.
"""
import csv
import json
import os
from pathlib import Path


def _config():
    cfg_path = os.environ.get('O2C_CONFIG') or os.environ.get('ICELAND_CONFIG')
    if not cfg_path:
        raise SystemExit('addr_csv needs O2C_CONFIG (or ICELAND_CONFIG) set to a build config')
    return json.load(open(cfg_path))


def _config_data():
    return _config()['data']


def _box():
    return tuple(_config()['addr']['box'])


def towns(path=None):
    p = path or os.path.expanduser(_config_data()['postcodes'])
    out = {}
    with open(p, encoding='utf-8-sig', newline='') as f:
        for row in csv.reader(f, delimiter=';'):
            if len(row) < 2 or not row[0].strip().isdigit():
                continue
            out[int(row[0])] = row[1].strip()
    return out


def addresses(path=None, box=None, municipalities=None):
    """Yield dicts for rows inside the box, or with municipalities (SVFNR strings) for
    the rows of those municipalities wherever they lie."""
    p = path or os.path.expanduser(_config_data()['addresses'])
    if municipalities is None:
        lo_lat, hi_lat, lo_lon, hi_lon = box or _box()
    with open(p, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row['N_HNIT_WGS84'])
                lon = float(row['E_HNIT_WGS84'])
            except (TypeError, ValueError):
                continue
            if municipalities is not None:
                if row['SVFNR'] not in municipalities:
                    continue
            elif not (lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon):
                continue
            street = (row['HEITI_NF'] or '').strip()
            if not street:
                continue
            yield dict(street=street, dative=(row['HEITI_TGF'] or '').strip(), svf=row['SVFNR'],
                       num=(row['HUSNR'] or '').strip(), letter=(row['BOKST'] or '').strip(),
                       post=int(row['POSTNR']) if (row['POSTNR'] or '').strip().isdigit() else 0,
                       lat=lat, lon=lon)


def collate(name):
    """Sort key in the register's own collation order (config['addr']['alphabet']).
    A letter outside it sorts after every letter."""
    alphabet = _config()['addr']['alphabet']
    order = {c: i for i, c in enumerate(alphabet)}
    return tuple(order.get(c, len(alphabet) + ord(c)) for c in name.upper())


def ladder():
    """(towns in row order, town -> its level-1 index).

    Both indices an ADDRE, ZCODE or ICODE record carries come from here, so the three
    files agree by construction.
    """
    tn = towns()
    seen = {tn[p] for p in {r['post'] for r in addresses()} if p in tn}
    rows = sorted(seen, key=collate)
    return rows, {t: i for i, t in enumerate(rows)}


def area_towns():
    """The ICODE town rows: the postcode towns of every address in the config's
    addr.area_municipalities, addr.poc_town first (row 0 is where a zero town index lands)
    and the rest in the register's collation."""
    cfg = _config()['addr']
    svf = {k for a in cfg['area_municipalities'].values() for k in a}
    tn = towns()
    found = {tn[r['post']].upper() for r in addresses(municipalities=svf) if r['post'] in tn}
    first = cfg['poc_town'].upper()
    return tuple([first] + sorted(found - {first}, key=collate))


def check_ladder():
    """The register's towns in the box are the ones config['addr']['towns_expected'] names.
    Raises if they drifted."""
    expected = tuple(_config()['addr']['towns_expected'])
    rows, town_of = ladder()
    if tuple(rows) != expected:
        missing = sorted(set(expected) - set(rows))
        extra = sorted(set(rows) - set(expected))
        raise SystemExit('town list drifted: missing %r extra %r order %r'
                         % (missing, extra, rows))
    return rows, town_of


if __name__ == '__main__':
    t = towns()
    rows = list(addresses())
    streets = {}
    for r in rows:
        streets.setdefault((r['street'], r['post']), []).append(r)
    print('rows', len(rows))
    print('distinct street names', len({r['street'] for r in rows}))
    print('distinct (street, postcode)', len(streets))
    print('postcodes', len({r['post'] for r in rows}))
    print('towns', len({t.get(r['post'], '?') for r in rows}))
    nonascii = sorted({r['street'] for r in rows if not r['street'].isascii()})
    print('street names with non-ASCII letters', len(nonascii))
    first = sorted({r['street'][0] for r in rows if not r['street'][0].isascii()})
    print('non-ASCII first letters', first)
    n1 = len({r['street'] for r in rows if not r['street'][0].isascii()})
    print('street names starting with one', n1)
    print('longest name', max(len(r['street']) for r in rows))
    print('max UTF-8 bytes', max(len(r['street'].encode()) for r in rows))
    rows, town_of = check_ladder()
    print('town rows', len(rows))
    for i, nm in enumerate(rows):
        print('  %2d %s' % (i, nm))
