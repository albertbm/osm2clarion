"""Build ADDRE013 and ZCODE013 for the southwest box from Stadfangaskra.

Iceland-specific, so it stays in leaf-is-map. The format writers it calls
(addre_io, zcode_io) are generic and belong in osm2clarion.

One record per (street, town) - not per (street, postcode), which wrote a town once
per code. Each carries its own area ladder, (country, town, town), which is what the
unit resolves the address to and what the street list prints in brackets. The town
index is a row `build_icode.py` puts into ICODE001 under area 12, so the two are built
from the same ladder.

The ladder is three levels long because area 12's ISO entry declares three tier
labels, and on every factory file measured the record length and that count agree -
Andorra three, the UK four, Italy five. An Icelandic address has no tier between the
town and the country, so the third level repeats the town, which is what the Vatican,
Monaco and San Marino do. Its index is 0 and that is a real row, not a marker: each
town row has exactly one child.

  python3 tools/addr/build_addr.py <out dir> [--keywidth 1|2]
"""
import argparse
import os
import sys
from pathlib import Path

# the format writers live in osm2clarion; this file only knows about Iceland
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import addre_io  # noqa: E402
import zcode_io  # noqa: E402
from addr_csv import addresses, check_ladder, towns  # noqa: E402

AREA = 12          # Italy's ordinal; the row the card renames to ISLAND
NLEVEL = 3         # area levels, which must equal the ISO entry's tier-label count

# Á, Í, Ó, Ú, Ö, Æ and the rest are the factory's own convention and live in
# addre_io.fold_key. Þ and Ð appear in no factory name, so these two are ours: the
# registry spells Þorvaldsensstræti "Thorvaldsensstræti" itself, and the keyboard has
# no Þ key for anything else to be typed on.
FOLD_IS = {'Þ': 'TH', 'þ': 'th', 'Ð': 'D', 'ð': 'd'}


def keyunits(s, kw):
    """UTF-8 bytes of each character, NUL-padded to kw. Skips what will not fit."""
    out = bytearray()
    for ch in s:
        b = ch.encode('utf-8')
        if len(b) > kw:
            return None
        out += b + b'\x00' * (kw - len(b))
    return bytes(out)


def house_items(nums):
    """The odd range then the even range, which is how the factory splits them.

    Andorra writes two items per street, side 0x54 for one and 0x58 for the other, and
    its side bytes decode as KIWI's valid-side flag: left-odd and left-even. So the
    numbers are grouped by parity rather than written as one span.
    """
    odd = sorted(n for n in nums if n % 2)
    even = sorted(n for n in nums if not n % 2)
    out = b''
    if odd:
        out += addre_io.house_item(odd[0], odd[-1], addre_io.SIDE_ODD)
    if even:
        out += addre_io.house_item(even[0], even[-1], addre_io.SIDE_EVEN)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--keywidth', type=int, default=2, choices=(1, 2))
    ap.add_argument('--no-fold', dest='fold', action='store_false',
                    help='keep the Icelandic letters in the search keys (unreachable '
                         'from the keyboard; it folds what is typed to ASCII)')
    ap.add_argument('--donor', required=True,
                     help='dir holding it013_prefix.bin, it013_defregion.bin and '
                          'it013z_prefix.bin, copied out of a factory-built card')
    a = ap.parse_args()
    kw = a.keywidth
    cr = 8 + kw + (4 if kw == 2 else 3)      # 12 for kw 1, 14 for kw 2
    assert cr % 2 == 0

    tn = towns()
    _towns, town_of = check_ladder()
    # One record per (street, town), not per (street, postcode). A town owns several
    # postcodes - 11 of our 21 do, Reykjavik fifteen - and the factory keys the record
    # by the town: Denmark gives 264 postcodes the one ladder of KOBENHAVN K, and
    # STEVENAGE, which has about 1,899 postcodes, is one row in the UK's area tree and
    # 8 ladders in ADDRE041, one per locality under it. Keying on the postcode instead
    # writes the town once per code, which is what put KOPAVOGUR in the City list four
    # times.
    # grouped on the upper-case name because that is what the record stores, and the
    # registry spells seven of them both ways - 'Midtun' and 'MIdtun' in Selfoss
    groups = {}
    for r in addresses():
        groups.setdefault((r['street'].upper(), tn.get(r['post'], '')), []).append(r)

    entries = []
    for (street, town), rows in sorted(groups.items()):
        ti = town_of[town]
        lat = sum(x['lat'] for x in rows) / len(rows)
        lon = sum(x['lon'] for x in rows) / len(rows)
        nums = [int(x['num']) for x in rows if x['num'].isdigit()]
        entries.append(dict(street=street, town=town,
                            posts=sorted({x['post'] for x in rows}),
                            levels=(AREA, ti, 0),
                            lat=lat, lon=lon, nums=nums, n=len(rows)))

    # the factory holds both keys and display names in upper case - Vatican, Andorra,
    # Belarus and Italy all do - and the keyboard only offers upper case
    names = []
    for i, e in enumerate(entries):
        ref = (i * 4 + 1) & 0xffff
        # KANREN and INFO, a start/end pair into the road data of the parcel the record's
        # own latitude and longitude names. Measured bounded at 676 to 16,859 whatever the
        # country's size, so nothing here can produce a real one; the pair stays
        # fabricated and above every factory value, which fails rather than resolving
        # somewhere wrong. REPORT-towns-resolve.md section 3.
        names.append(addre_io.name_record(
            e['street'].upper(), *addre_io.parcel_words(e['lat'], e['lon']), e['levels'],
            house_items(e['nums']), refs=((2, 0x40, ref), (3, 0x40, (ref + 4) & 0xffff))))

    # one key per word start of each street name, the whole remainder from that word
    bykey = {}
    skipped = 0
    unfolded = set()
    for i, e in enumerate(entries):
        s = e['street'].upper()
        search = addre_io.fold_key(s, FOLD_IS).upper() if a.fold else s
        if a.fold:
            unfolded |= {c for c in search if not c.isascii()}
        starts = [0] + [j + 1 for j, c in enumerate(search) if c == ' ' and j + 1 < len(search)]
        for p in starts:
            if not search[p].isalnum():
                continue
            u = keyunits(search[p:], kw)
            if u is None:
                skipped += 1
                continue
            hit = (i, 0x0000 if p == 0 else 0x4000)
            if hit not in bykey.setdefault(u, []):
                bykey[u].append(hit)
    assert not unfolded, 'no folding for %r' % sorted(unfolded)

    keys = []
    srecs = []
    for u in sorted(bykey):
        hits = sorted(bykey[u])
        keys.append((u, len(srecs), len(hits)))
        for i, fl in hits:
            srecs.append(addre_io.street_record(fl, i, entries[i]['levels']))
    # the factory writes the last record with area 0 and the trailing fields zeroed
    srecs[-1] = srecs[-1][:12] + b'\x00' * 10

    prefix = Path(a.donor, 'it013_prefix.bin').read_bytes()
    defreg = Path(a.donor, 'it013_defregion.bin').read_bytes()
    # the factory prologue is the first street record's own area vector: Andorra and the
    # Vatican repeat those ten bytes exactly, Greece and Italy zero the deepest level
    pre = srecs[0][12:22]
    assert len(pre) == 10
    # the donor's D and name-width hint belong to Italy's content, not Iceland's
    out, blocks = addre_io.assemble(
        prefix, keys, srecs, names, defreg, pre, cr, kw, AREA, NLEVEL - 2,
        maxkey_hint=max(len(k[0]) for k in keys) // 2 + 1,
        namewidth_hint=max(len(n) for n in names) // 2)

    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    (od / 'ADDRE013.DAT').write_bytes(out)

    # A postcode belongs to exactly one town, which is what the ZCODE record carries, so
    # the 52 codes share 21 ladders. Andorra leaves the levels a postcode does not reach
    # at 0xffff, which is what zcode_io pads with, so only the three Iceland fills are
    # passed and the four slots past them stay 0xffff.
    levels = {}
    for e in entries:
        for post in e['posts']:
            levels[str(post)] = e['levels']
    z = zcode_io.assemble(Path(a.donor, 'it013z_prefix.bin').read_bytes(),
                          sorted(levels), levels)
    (od / 'ZCODE013.DAT').write_bytes(z)

    print('entries (street, town) %d  distinct names %d  towns %d  postcodes %d' % (
        len(entries), len({e['street'] for e in entries}), len({e['town'] for e in entries}),
        len(levels)))
    print('keys %d  street records %d  name records %d  skipped keys %d' % (
        len(keys), len(srecs), len(names), skipped))
    roots = sorted({k[0][:kw].rstrip(b'\x00').decode('utf-8', 'replace') for k in keys})
    print('keys ASCII %s  root children %d: %s' % (
        all(k[0].decode('utf-8', 'replace').replace('\x00', '').isascii() for k in keys),
        len(roots), ' '.join(roots)))
    print('area levels %d (ISO entry 12 declares three)  distinct vectors %d  town rows %s' % (
        NLEVEL,
        len({e['levels'] for e in entries}), sorted({e['levels'][1] for e in entries})))
    print('ADDRE013.DAT %d bytes, %d name blocks; ZCODE013.DAT %d bytes, %d codes' % (
        len(out), len(blocks), len(z), len(levels)))


if __name__ == '__main__':
    main()
