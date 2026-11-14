#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One country's ADDRE file for a factory card whose roads were rebuilt in one area.

The card keeps its ICODE area tree, so nothing but street search changes: every record
carries the ladder the tree already has for [addr] municipality, and the settlement under
it that the address's town names (the one named like the municipality when it names none).
A street gets one row per road link and side with the house numbers found along it, or a
plain row when it has no numbers. Each row points at a link of the street's own drawn shape
in the rebuilt tile, through the shape index rd_build.py --shape-index wrote.

Only addresses inside the area's relations are read, since only those roads were rebuilt.
A street with no road of its name in the tile is left out and listed in addr_skipped.json.
The country's other address files (ZCODE, INTER, POINT) are not written and stay the card's.

    build_addr_country.py OUTDIR --profile NAME --area AREA --card CARD.img --shapes TILE.shapes.json --pbf FILE

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


def rows(P, pbf, area):
    """The address rows inside the area's relations."""
    cfg = P.raw['addr']
    if cfg.get('format', 'osm') != 'osm':
        raise SystemExit('[addr] format %r: this step reads OSM addresses' % cfg['format'])
    inside = osm_roads.predicates(osm_roads.region(pbf, relations=set(area['relations'])))[0]
    return [r for r in addr_sources.osm_addresses(pbf, tuple(cfg['box']))
            if inside((r['lon'], r['lat'])) and int(r['num']) <= MAX_NUMBER]


def entries(found, settlements, home, text):
    """One entry per street and settlement, sorted by search key. `text` spells a name as the card does."""
    groups = collections.defaultdict(list)
    for r in found:
        town = text(r['town'].upper())
        groups[(text(r['street'].upper()), town if town in settlements else home)].append(r)
    out, odd = [], []
    for (name, town), rs in groups.items():
        key = addre_io.fold_key(name).upper()
        if not key.isascii() or not addr_links.word_keys(key):
            odd.append(dict(street=name, town=town, addresses=len(rs), why='no key the keyboard can type'))
            continue
        nums = sorted({int(r['num']) for r in rs if r['num'].isdigit() and int(r['num'])})
        low = [r for r in rs if nums and r['num'] == str(nums[0])] or rs
        lat, lon = sum(r['lat'] for r in rs) / len(rs), sum(r['lon'] for r in rs) / len(rs)
        out.append(dict(street=name, name=name, key=key, town=town, nums=nums, addresses=rs, n=len(rs),
                        posts=sorted({r['post'] for r in rs if r['post']}), lat=lat, lon=lon,
                        anchor=(lon, lat), low=(low[0]['lon'], low[0]['lat'])))
    return sorted(out, key=lambda e: (e['key'], e['town'])), odd


def place(links, ents):
    """Give each entry its ranges or its link. Returns (placed, skipped)."""
    placed, skipped = [], []
    for e in ents:
        ranges, _ = addr_links.address_ranges(links, e)
        if ranges:
            e['ranges'] = sorted(ranges, key=lambda r: (r['lo'], r['hi']))    # the factory's order
        else:
            got = addr_links.pick(links, e)
            if got is None or got[0] != 'name':
                skipped.append(dict(street=e['name'], town=e['town'], addresses=e['n'], lat=e['lat'], lon=e['lon'],
                                    why='no road of this name in the rebuilt tile'))
                continue
            e['link'] = addr_links.link_refs(e, *got)
        placed.append(e)
    return placed, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--profile', required=True)
    ap.add_argument('--area', required=True)
    ap.add_argument('--card', required=True, help='the base card image; read for ICODE001.DAT and the file layout')
    ap.add_argument('--shapes', nargs='+', required=True)
    ap.add_argument('--pbf', required=True)
    a = ap.parse_args()
    P = clarion_profile.load(a.profile)
    fmt = P.fmt
    municipality = P.raw['addr']['municipality']
    with fatread.Fat32(a.card) as fs:
        ic, _ = card_file(fs, '/REFER001/ICODE001.DAT')
        ordinal, path, settlements, labels = ladder(ic, P.country, municipality, fmt.codec)
        name = 'ADDRE%03d.DAT' % (ordinal + 1)
        factory, packed = card_file(fs, '/REFER001/' + name)
    old = addre_io.Addre(factory)
    if old.area != ordinal or old.ntier + 2 != len(labels):
        raise SystemExit('%s on the card is area %d with %d levels, not country %d' % (name, old.area, old.ntier + 2, P.country))
    version = bytes(ic[0xfa:0xfa + 22]).decode('utf-16-be')
    home = municipality if municipality in settlements else None
    none = len(settlements)                # the factory's marker for no settlement
    print('%s: ordinal %d, %s is ladder %s of %d levels with %d settlements, DATA VERSION %s'
          % (name, ordinal, municipality, path, len(labels), none, version))

    def text(s):
        return fmt.text(s).decode(fmt.codec)
    found = rows(P, a.pbf, P.areas[a.area])
    ents, odd = entries(found, settlements, home, text)
    links = addr_links.Links([R for p in a.shapes for R in json.load(open(p))['records']])
    ents, skipped = place(links, ents)
    if not ents:
        raise SystemExit('no address matched a road of the rebuilt tile')

    def ladder_of(e):
        return path + (settlements.get(e['town'], none),)
    out, keys, blocks = addr_links.addre_mixed(
        ents, ladder_of, lambda n: addr_links.word_keys(addre_io.fold_key(n).upper()),
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
        [dict(street=e['name'], town=e['town'], ladder=ladder_of(e), posts=e['posts'], addresses=e['n'],
              **(dict(ranges=[{k: r[k] for k in r if k != 'points'} for r in e['ranges']]) if 'ranges' in e
                 else dict(link=e['link']))) for e in ents], ensure_ascii=False, indent=1))
    (od / 'addr_skipped.json').write_text(json.dumps(skipped + odd, ensure_ascii=False, indent=1))
    ranged = [e for e in ents if 'ranges' in e]
    per = collections.Counter(e['town'] or '(none)' for e in ents)
    print('addresses in the area %d; streets %d (%d addresses), %d with house-number ranges (%d ranges), %d plain'
          % (len(found), len(ents), sum(e['n'] for e in ents), len(ranged), sum(len(e['ranges']) for e in ranged),
             len(ents) - len(ranged)))
    print('left out: %d streets with no road of their name (%d addresses), %d with no typeable key'
          % (len(skipped), sum(s['addresses'] for s in skipped), len(odd)))
    print('settlements: %s' % ', '.join('%s %d' % kv for kv in per.most_common()))
    print('%s %d B (factory %d B), %d keys, %d name blocks%s'
          % (name, len(out), len(factory), len(keys), len(blocks), ', packed' if packed else ''))


if __name__ == '__main__':
    main()
