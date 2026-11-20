#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One country's ADDRE file for a factory card whose roads were rebuilt in one area.

The card keeps its ICODE area tree, so nothing but street search changes. Two ways to place
an address in that tree:

- the area names none: each address goes to the kunta and settlement the card's own factory
  street file points to (settle.py), from the municipality number a register row carries.
  This is what the Finland profile does, for one town or the whole country;
- the area names a municipality ([areas.NAME] municipality): every record carries the ladder
  the tree has for it, and the settlement under it that the address's town names (the one
  named like the municipality when it names none).

The addresses come from [addr] source: "osm" reads the pbf, "register" the CSV at [data]
addresses in the [addr] register layout. Only addresses inside the area's relations are read,
since only those roads were rebuilt. A register row with a Swedish street name as well
(Ryhti's street_swe) gives a street under each name, as the factory file lists both.

A street gets one row per road link with the house numbers found along it, both sides of
the road in the one row as the factory writes them, or a plain row when it has no numbers.
Each row points at a link of the street's own drawn shape in the rebuilt tile, through the
shape index rd_build.py --shape-index wrote. A street with no road of its name in the tile
is left out and listed in addr_skipped.json; one whose road carries its name in the other
language goes to addr_other_language.json.
The country's other address files (ZCODE, INTER, POINT) are not written and stay the card's.

    build_addr_country.py OUTDIR --profile NAME --area AREA --card CARD.img --shapes TILE.shapes.json
                          --pbf FILE [--addresses FILE.csv]

Writes ADDRE<ordinal + 1>.DAT, as a .cmp/.ind pair too when the card stores it packed, and
card.json naming what goes on the card.
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / 'addr'))
import addr_links  # noqa: E402
import addr_sources  # noqa: E402
import addre_io  # noqa: E402
import card_pack  # noqa: E402
import clarion_profile  # noqa: E402
import fatread  # noqa: E402
import frames  # noqa: E402
import icode_io  # noqa: E402
import osm_roads  # noqa: E402
import settle  # noqa: E402

MAX_NUMBER = 9999      # a longer house number is a mistagged postcode or phone number


def card_file(fs, path):
    """(plain bytes, stored packed) of a card file that may be a .cmp/.ind pair."""
    try:
        return fs.read(path), False
    except FileNotFoundError:
        return card_pack.unwrap(fs.read(path + '.cmp'), fs.read(path + '.ind')), True


def ladder(ic, iso, municipality, codec):
    """(ordinal, the municipality's ladder, {settlement name: index}, tier labels).

    The municipality is the row one level above the deepest, wherever it sits in the country."""
    ordinal = icode_io.iso_ordinal(ic, iso)
    labels = icode_io.iso_entry(ic, ordinal)[1]
    _lang, base = icode_io.area_frames(ic)[0]
    found = []

    def walk(row, path):
        for r in icode_io.children(ic, base, row):
            if len(path) + 2 < len(labels):
                walk(r, path + [r.idx])
            elif r.name.decode(codec) == municipality:
                found.append((path + [r.idx], r))
    walk(icode_io.read_list(ic, base)[ordinal], [ordinal])
    if len(found) != 1:
        raise SystemExit('%d rows named %s at the %s level of country %d' % (len(found), municipality, labels[-2], iso))
    path, row = found[0]
    return ordinal, tuple(path), {r.name.decode(codec): r.idx for r in icode_io.children(ic, base, row)}, labels


def rows(P, pbf, area, addresses=None):
    """The address rows inside the area's relations, from the profile's address source."""
    import numpy as np
    import shapely
    cfg = P.raw['addr']
    kind, layout = addr_sources.source(cfg, default='osm')
    if kind == 'osm':
        found = list(addr_sources.osm_addresses(pbf, tuple(cfg['box'])))
    elif layout == 'ryhti':
        if not addresses or not os.path.exists(addresses):
            raise SystemExit('[addr] register "ryhti" needs the CSV ([data] addresses or --addresses): %s' % addresses)
        box = area.get('bbox')      # lon_min, lat_min, lon_max, lat_max: skips most of a country's rows
        found = list(addr_sources.ryhti_addresses(addresses, box and (box[1], box[3], box[0], box[2])))
    else:
        raise SystemExit('[addr] register %r: area mode reads "ryhti" or OSM' % layout)
    g = osm_roads.region(pbf, relations=set(area['relations']))
    shapely.prepare(g)
    inside = shapely.contains_xy(g, np.array([r['lon'] for r in found]), np.array([r['lat'] for r in found]))
    return [r for r, ok in zip(found, inside) if ok and int(r['num']) <= MAX_NUMBER]


def names_of(r, fold):
    """The street names an address is found under: its own, and a second-language one."""
    out = [r['street']]
    other = r.get('street_swe') or ''
    if other and fold(other) != fold(r['street']):
        out.append(other)
    return out


def entries(found, settlements, home, text):
    """One entry per street and settlement, sorted by search key. `text` spells a name as the card does."""
    def town_of(r):
        town = text(r['town'].upper())
        return (town if town in settlements else home), None
    return group(found, town_of, text, lambda r: [r['street']])


def group(found, town_of, text, names):
    """Entries by street name and (town, ladder) from town_of(r); names(r) lists the names an
    address is found under."""
    groups = collections.defaultdict(list)
    for r in found:
        town, lad = town_of(r)
        for nm in names(r):
            groups[(text(nm.upper()), town, lad)].append(r)
    out, odd = [], []
    for (name, town, lad), rs in groups.items():
        key = addre_io.fold_key(name).upper()
        if not key.isascii() or not addr_links.word_keys(key):
            odd.append(dict(street=name, town=town, addresses=len(rs), why='no key the keyboard can type'))
            continue
        nums = sorted({int(r['num']) for r in rs if r['num'].isdigit() and int(r['num'])})
        low = [r for r in rs if nums and r['num'] == str(nums[0])] or rs
        lat, lon = sum(r['lat'] for r in rs) / len(rs), sum(r['lon'] for r in rs) / len(rs)
        out.append(dict(street=name, name=name, key=key, town=town, ladder=lad, nums=nums, addresses=rs, n=len(rs),
                        posts=sorted({r['post'] for r in rs if r['post']}), lat=lat, lon=lon,
                        anchor=(lon, lat), low=(low[0]['lon'], low[0]['lat'])))
    return sorted(out, key=lambda e: (e['key'], e['ladder'] or (), e['town'] or '')), odd


def place(links, ents, reach=None):
    """Give each entry its ranges or its link. Returns (placed, skipped). With `reach` only a
    road of the name that close to an address counts (a whole country has many of each name)."""
    placed, skipped = [], []
    for e in ents:
        if ':' in e['name']:
            # a label reads a colon as the break between two names; the factory label
            # writes KAARLE IX'N KATU for the street KAARLE IX:N KATU, ours keep the colon
            skipped.append(dict(street=e['name'], town=e['town'], ladder=e['ladder'], addresses=e['n'],
                                lat=e['lat'], lon=e['lon'], why='a colon in the name, which the road label '
                                'reads as two names', rows=e['addresses']))
            continue
        ranges, e['unmatched'] = addr_links.address_ranges(links, e, reach)
        if ranges:
            e['ranges'] = sorted(ranges, key=lambda r: (r['lo'], r['hi']))    # the factory's order
        else:
            got = addr_links.pick(links, e, local=reach is not None)
            link = addr_links.link_refs(links, e, *got) if got and got[0] == 'name' else None
            if link is None:
                skipped.append(dict(street=e['name'], town=e['town'], ladder=e['ladder'], addresses=e['n'],
                                    lat=e['lat'], lon=e['lon'], why='no road of this name in the rebuilt tile',
                                    rows=e['addresses']))
                continue
            e['link'] = link
            e['nums'] = []      # a factory row with no link per number holds no numbers
        placed.append(e)
    return placed, skipped


def other_language(skipped, placed, text, fold):
    """(no road of the name, a road with the street's other-language name): skipped entries
    split by whether the same addresses were placed under their other name."""
    have = {(e['name'], e['ladder']) for e in placed}
    missing, other = [], []
    for s in skipped:
        rs = s.pop('rows')
        # either language: in a Swedish-speaking town the road carries the Swedish name
        partner = any(text(n.upper()) != s['street'] and (text(n.upper()), s['ladder']) in have
                      for r in rs for n in names_of(r, fold))
        (other if partner else missing).append(s)
    return missing, other


def by_settlement(P, a, ic, factory, labels, found, fmt):
    """(town_of, names, report lines) for a whole country, each address placed by settle.py."""
    fold = settle.folder(fmt)
    tree, _ = settle.area_tree(ic, P.country, fmt.codec)
    recs = settle.factory_records(factory, fmt, tree)
    S = settle.Settler(tree, recs, addr_sources.osm_municipalities(a.pbf), fold)
    out, home = S.assign(found)
    names = {k: {i: n for n, i in s.items()} for k, (_, s) in tree.items()}
    got = {id(r): (k, s) for r, (k, s, _, _) in zip(found, out)}

    def town_of(r):
        k, s = got[id(r)]
        return names[k].get(s, k), tree[k][0] + (s,)
    lines = ['factory: %d kunta rows, %d name records (%d with no parcel position); %d municipality numbers matched'
             % (len(tree), len(recs), sum(r[3] is None for r in recs), len(S.kunta_of)),
             'merged away since the card: %s' % (', '.join('%s (now %d)' % kv for kv in sorted(home.items())) or 'none in these rows')]
    lines += ['  %-32s %8d addresses %7d streets' % (p, *n) for p, n in settle.counts(found, out, fold).items()]
    return town_of, (lambda r: names_of(r, fold)), lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--profile', required=True)
    ap.add_argument('--area', required=True)
    ap.add_argument('--card', required=True, help='the base card image; read for ICODE001.DAT and the file layout')
    ap.add_argument('--shapes', nargs='+', required=True)
    ap.add_argument('--pbf', required=True)
    ap.add_argument('--addresses', help='the register CSV; [data] addresses when left out')
    a = ap.parse_args()
    P = clarion_profile.load(a.profile)
    fmt = P.fmt
    area = P.areas[a.area]
    if 'municipality' in P.raw['addr']:
        raise SystemExit('[addr] municipality has moved to the area: [areas.%s] municipality' % a.area)
    municipality = area.get('municipality')
    addresses = a.addresses or P.resolve(P.raw.get('data', {}).get('addresses'))
    with fatread.Fat32(a.card) as fs:
        ic, _ = card_file(fs, '/REFER001/ICODE001.DAT')
        ordinal = icode_io.iso_ordinal(ic, P.country)
        labels = icode_io.iso_entry(ic, ordinal)[1]
        name = 'ADDRE%03d.DAT' % (ordinal + 1)
        factory, packed = card_file(fs, '/REFER001/' + name)
    old = addre_io.Addre(factory)
    if old.area != ordinal or old.ntier + 2 != len(labels):
        raise SystemExit('%s on the card is area %d with %d levels, not country %d' % (name, old.area, old.ntier + 2, P.country))
    version = bytes(ic[0xfa:0xfa + 22]).decode('utf-16-be')

    def text(s):
        return fmt.text(s).decode(fmt.codec)
    found = rows(P, a.pbf, area, addresses)
    if municipality:
        _, path, settlements, _ = ladder(ic, P.country, municipality, fmt.codec)
        home = municipality if municipality in settlements else None
        none = len(settlements)                # the factory's marker for no settlement
        print('%s: ordinal %d, %s is ladder %s of %d levels with %d settlements, DATA VERSION %s'
              % (name, ordinal, municipality, path, len(labels), none, version))
        ents, odd = entries(found, settlements, home, text)
        for e in ents:
            e['ladder'] = path + (settlements.get(e['town'], none),)
        reach, links = None, addr_links.Links([R for p in a.shapes for R in json.load(open(p))['records']])
    else:
        print('%s: ordinal %d, area %s placed by the factory street file, %d levels, DATA VERSION %s'
              % (name, ordinal, a.area, len(labels), version))
        town_of, names, lines = by_settlement(P, a, ic, old, labels, found, fmt)
        print('\n'.join(lines))
        ents, odd = group(found, town_of, text, names)
        reach, links = addr_links.NAME_REACH, addr_links.Links([], named_only=True)
        for p in a.shapes:
            for R in json.load(open(p))['records']:
                links.add(R, named_only=True)
    ents, skipped = place(links, ents, reach)
    if not ents:
        raise SystemExit('no address matched a road of the rebuilt tile')
    skipped, other = other_language(skipped, ents, text, settle.folder(fmt))

    out, keys, blocks = addr_links.addre_mixed(
        ents, lambda e: e['ladder'], lambda n: addr_links.word_keys(addre_io.fold_key(n).upper()),
        frames.addre_prefix(P.bounds, version, fmt), frames.addre_df(fmt), ordinal, len(labels) - 2, fmt=fmt, shorten=fmt.refer_v5)

    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    (od / name).write_bytes(out)
    files = {'REFER001/' + name: name}
    if packed:
        c, i = card_pack.wrap(out)
        (od / (name + '.cmp')).write_bytes(c)
        (od / (name + '.ind')).write_bytes(i)
        files = {'REFER001/%s.%s' % (name, x): '%s.%s' % (name, x) for x in ('cmp', 'ind')}
    (od / 'card.json').write_text(json.dumps(dict(files=files, index={'REFER001:' + name: len(out)}), indent=1))
    (od / 'addr_records.json').write_text(json.dumps(
        [dict(street=e['name'], town=e['town'], ladder=e['ladder'], posts=e['posts'], addresses=e['n'],
              **(dict(ranges=[{k: r[k] for k in r if k != 'points'} for r in e['ranges']]) if 'ranges' in e
                 else dict(link=e['link']))) for e in ents], ensure_ascii=False, indent=1))
    (od / 'addr_skipped.json').write_text(json.dumps(skipped + odd + links.cut, ensure_ascii=False, indent=1))
    (od / 'addr_other_language.json').write_text(json.dumps(other, ensure_ascii=False, indent=1))
    ranged = [e for e in ents if 'ranges' in e]
    per = collections.Counter(e['town'] or '(none)' for e in ents)
    far = sum(len(e['unmatched']) for e in ents if 'ranges' in e)
    print('addresses in the area %d; streets %d (%d addresses), %d with house-number ranges (%d ranges), %d plain'
          % (len(found), len(ents), sum(e['n'] for e in ents), len(ranged), sum(len(e['ranges']) for e in ranged),
             len(ents) - len(ranged)))
    print('left out: %d streets with no road of their name (%d addresses), %d with no typeable key, '
          '%d names whose road is labelled in the other language (%d addresses)'
          % (len(skipped), sum(s['addresses'] for s in skipped), len(odd), len(other), sum(s['addresses'] for s in other)))
    print('links no record can follow over a parcel line: %d (%d addresses)'
          % (len(links.cut), sum(c['addresses'] for c in links.cut)))
    if reach:
        print('addresses farther than %d m from every road of their street, on a street that has one: %d' % (reach, far))
    print('settlements: %s' % ', '.join('%s %d' % kv for kv in per.most_common(25)))
    print('%s %d B (factory %d B), %d keys, %d name rows, %d name blocks%s'
          % (name, len(out), len(factory), len(keys), sum(len(e.get('ranges', [1])) for e in ents), len(blocks),
             ', packed' if packed else ''))


if __name__ == '__main__':
    main()
