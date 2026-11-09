"""Build POINT013, the place-name index for the City keyboard.

Iceland-specific, so it stays in leaf-is-map; the record layout it writes lives in
osm2clarion's point_io.

`POINT013.DAT` is an 8-byte stub on every card this project has built, and 013 is the
only one of the 42 countries where that is true, which is why every letter of the City
keyboard is grey. The file written here holds one settlement record per town: 21 names,
the area ladder `build_icode.py` put in the tree, and a coordinate averaged over the
town's rows in Stadfangaskra.

No facilities. The factory files carry hotels, restaurants and petrol stations in the
same trie, and nothing in this build knows where any of them are.

    python3 tools/addr/build_point.py <out dir> --donor DIR
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import addre_io  # noqa: E402
import point_io  # noqa: E402
from build_addr import FOLD_IS, NLEVEL, keyunits  # noqa: E402
from addr_csv import addresses, check_ladder, towns  # noqa: E402

AREA = 12
KW = 2             # two bytes per key character, as every factory POINT file writes
CR = 14            # child-record size, likewise

# The two words after the coordinate are KANREN and INFO, the same pair of map
# references the street records carry, and the same measurement applies: they run 33 to
# 13,621 across the Vatican's, Andorra's, Denmark's and Greece's files whatever the
# country's size, so they are scoped to the map and nothing here can produce a real
# one. The base is above every value measured, so a lookup fails rather than resolving
# to a real but wrong link.
REF_BASE = 0x8000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--donor', required=True,
                     help='dir holding point_prefix.bin and point_defregion.bin, '
                          'copied out of a factory-built card')
    a = ap.parse_args()

    tn = towns()
    rows, town_of = check_ladder()
    pts = {}
    for r in addresses():
        t = tn.get(r['post'])
        if t:
            pts.setdefault(t, []).append((r['lat'], r['lon']))
    assert set(pts) == set(town_of), sorted(set(town_of) ^ set(pts))

    # The factory writes a settlement once, in the country's own language: 5,734 of
    # Denmark's 5,797 settlement records and 15,405 of Greece's 15,992 carry language
    # 63, and names that exist only at 63 - AIXIRIVALL in Andorra - are the rule rather
    # than the exception. So one record per town, not one per language, which is also
    # what keeps a town out of the list twice.
    entries = []
    for name in rows:
        lat = sum(p[0] for p in pts[name]) / len(pts[name])
        lon = sum(p[1] for p in pts[name]) / len(pts[name])
        lw, ow, iy, ix = addre_io.parcel_point(lat, lon)
        entries.append(dict(
            name=name.upper(), idx=town_of[name], lat=lw, lon=ow,
            x=ix, y=iy,
            levels=(AREA, town_of[name], 0), rows=len(pts[name])))

    # one key per town, folded to the keyboard's own alphabet; a word start that is not
    # alphanumeric is skipped, which is what the street build does and what drops the
    # bracket of GARDABAER (ALFTANES)
    bykey = {}
    for i, e in enumerate(entries):
        search = addre_io.fold_key(e['name'], FOLD_IS).upper()
        assert search.isascii(), (e['name'], search)
        starts = [0] + [j + 1 for j, c in enumerate(search)
                        if c == ' ' and j + 1 < len(search)]
        for p in starts:
            if not search[p].isalnum():
                continue
            u = keyunits(search[p:], KW)
            hit = (i, 0x0000 if p == 0 else 0x4000)
            if hit not in bykey.setdefault(u, []):
                bykey[u].append(hit)

    keys = []
    recs = []
    names = []
    order = []
    for u in sorted(bykey):
        hits = sorted(bykey[u])
        keys.append((u, len(recs), len(hits)))
        for i, fl in hits:
            order.append((i, fl))
            recs.append(None)
    # the name record index is the record's own position, so the two arrays stay paired
    for slot, (i, fl) in enumerate(order):
        e = entries[i]
        ref = REF_BASE + slot * 4
        names.append(point_io.name_record(
            e['name'], e['levels'], NLEVEL, point_io.SETTLEMENT,
            lat=e['lat'], lon=e['lon'], x=e['x'], y=e['y'],
            refs=((2, 0x40, ref), (3, 0x40, ref + 1))))
        recs[slot] = point_io.data_record(
            e['levels'], NLEVEL, slot, point_io.SETTLEMENT,
            lat=e['lat'], lon=e['lon'], x=e['x'], y=e['y'],
            flags=fl)

    prefix = Path(a.donor, 'point_prefix.bin').read_bytes()
    defreg = Path(a.donor, 'point_defregion.bin').read_bytes()
    out, nblocks, tblocks = point_io.assemble(
        prefix, defreg, keys, recs, names, [], CR, KW, AREA, NLEVEL,
        maxkey_hint=max(len(k[0]) for k in keys) // 2 + 1)

    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    (od / 'POINT013.DAT').write_bytes(out)
    print('towns %d  keys %d  records %d  name records %d  name blocks %d' % (
        len(entries), len(keys), len(recs), len(names), len(nblocks)))
    print('POINT013.DAT %d bytes, area %d, ladder %d levels, category %#02x' % (
        len(out), AREA, NLEVEL, point_io.SETTLEMENT))
    for e in entries:
        print('  %2d %-22s %5d rows  lat %#06x lon %#06x  ladder %s'
              % (e['idx'], e['name'], e['rows'], e['lat'], e['lon'], e['levels']))


if __name__ == '__main__':
    main()
