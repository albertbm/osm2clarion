#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One country's POI search (POINT<nnn>.DAT, X08-02) from OSM, on the roads of our tiles.

The card's settlements (category 31) stay, since the area tree and the ADM frame are
built on them, but each gets new references: the nearest link in its own level-1 parcel,
or in one of the eight around it when its own has no road. Every factory facility goes.
The facilities are extract_pois.py's (restaurants, hotels, hospitals and rest areas, with
the codes the Iceland cards use), each referenced to the nearest link in its own parcel
and moved onto that link, as poi_point.py does; one in a parcel with no road is left out.

The card keeps its ICODE area tree. A facility's ladder is the ladder of the nearest
record in the card's own POINT file: the tree has no geometry of its own.

    build_point.py OUTDIR --profile NAME --card CARD.img --pois POIS.json --tiles TILE.DAT ...

Writes POINT<ordinal + 1>.DAT, as a .cmp/.ind pair when the card stores it packed, and
card.json naming what goes on the card.
"""
import argparse
import collections
import json
import math
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / 'addr'))
import addr_links  # noqa: E402
import addre_io  # noqa: E402
import clarion_bg  # noqa: E402
import clarion_profile  # noqa: E402
import fatread  # noqa: E402
import icode_io  # noqa: E402
import inter_io  # noqa: E402
import point_v5  # noqa: E402
import poi_point  # noqa: E402
from addr_resolve import code_rc  # noqa: E402
from build_inter import card_file  # noqa: E402

SQUEEZE = math.cos(math.radians(62))


def point_ll(la, lo, x, y):
    row, col = code_rc(la, lo)
    return (row + y / 2048) / 48, (col + x / 2048) / 32 - 924


class Ladders:
    """The ladder at a point: that of the nearest record in the card's own POINT file."""

    def __init__(self, fields):
        from shapely import STRtree, points
        self.lvs = [f['levels'] for f in fields]
        self.tree = STRtree(points([(lon * SQUEEZE, lat) for lat, lon in
                                    (point_ll(f['lat'], f['lon'], f['xw'] & 0x7ff, f['y']) for f in fields)]))
        self.point = points

    def __call__(self, lat, lon):
        return self.lvs[int(self.tree.nearest(self.point(lon * SQUEEZE, lat)))]


def link_refs(tile_recs, la, lo, x, y, lat, around=False):
    """(refs, foot x, foot y, parcel code) of the nearest link to a point, in its own parcel
    or, with around, the nearest of its own and the eight around it; None when none has a road."""
    rc = code_rc(la, lo)
    best = None
    for dr, dc in [(0, 0)] + ([(a, b) for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b] if around else []):
        code = clarion_bg.parcel_code(1, rc[0] + dr, rc[1] + dc)
        rec = tile_recs.get(code)
        got = poi_point.nearest_link_refs(rec[0], rec[1], x - 2048 * dc, y - 2048 * dr, lat) if rec else None
        if got is None:
            continue
        refs, (fx, fy) = got
        mx, my = poi_point.parcel_metres(lat)
        dist = math.hypot((fx - (x - 2048 * dc)) * mx, (fy - (y - 2048 * dr)) * my)
        if best is None or dist < best[0]:
            best = (dist, refs, fx, fy, code)
        if not around:
            break
    return None if best is None else best[1:]


def build(old, pois, tile_recs, fmt):
    """A point_v5.Point-shaped object with the new content, and the stats."""
    fields = [point_v5.parse_name_record(r) for r in old.names]
    lang_of = {point_v5.parse_row(r)['name_idx']: point_v5.parse_row(r)['lang'] for r in old.rows}
    ladder = Ladders(fields)
    recs = []           # (record fields, language, phone)
    st = collections.Counter()
    skipped = []
    for i, f in enumerate(fields):
        if f['category'] != point_v5.SETTLEMENT:
            continue
        lat, _lon = point_ll(f['lat'], f['lon'], f['xw'] & 0x7ff, f['y'])
        got = link_refs(tile_recs, f['lat'], f['lon'], f['xw'] & 0x7ff, f['y'], lat, around=True)
        if got is None:
            skipped.append(dict(name=point_v5.text(f['name']).decode(fmt.codec), why='no road in or around its parcel'))
            st['settlements without a road'] += 1
            continue
        refs, _fx, _fy, (gla, glo) = got
        recs.append((dict(f, refs=refs, glat=gla, glon=glo), lang_of[i], None))
        st['settlements'] += 1
    for p in pois:
        la, lo, y, x = addre_io.parcel_point(p['lat'], p['lon'])
        got = link_refs(tile_recs, la, lo, x, y, p['lat'])
        if got is None:
            skipped.append(dict(name=p['name'], id=p['id'], why='no road in its parcel'))
            st['facilities without a road'] += 1
            continue
        refs, fx, fy, _code = got
        name = fmt.text(p['name'].upper())
        key = point_v5.search_key(name.decode(fmt.codec))
        if not key or not key.isascii():
            skipped.append(dict(name=p['name'], id=p['id'], why='no key the keyboard can type'))
            continue
        x, y = max(0, min(2047, round(fx))), max(0, min(2047, round(fy)))
        recs.append((dict(name=name, levels=ladder(p['lat'], p['lon']), category=p['cat'], subcategory=p['sub'],
                          lat=la, lon=lo, xw=0x800 | x, y=y, glat=la, glon=lo, refs=refs, items=b'', address=b'',
                          tel=(0xffff, 0xffff), flags=0x0c, tail=None), point_v5.LANG_LOCAL, p.get('phone')))
        st['facilities'] += 1
    recs.sort(key=lambda t: (t[0]['category'], t[0]['name'], t[0]['levels'], t[0]['lat'], t[0]['lon'], t[0]['xw'], t[0]['y']))
    names, infos, langs = [], [], []
    for f, lang, phone in recs:
        if phone:
            f = dict(f, tel=(0, len(infos)))
            infos.append(point_v5.info_record(phone.encode('ascii')))
        names.append(point_v5.name_record(**f))
        langs.append(lang)
    fields = [point_v5.parse_name_record(r) for r in names]

    def full_keys(sel):
        bykey = collections.defaultdict(list)
        for i, f in enumerate(fields):
            if not sel(f):
                continue
            for u, _later in inter_io.word_keys(point_v5.search_key(point_v5.text(f['name']).decode(fmt.codec))):
                bykey[u].append(i)
        keys, rows = [], []
        for u in sorted(bykey):
            hits = sorted(set(bykey[u]), key=lambda i: (addre_io.fold_key(point_v5.text(fields[i]['name']).decode(fmt.codec)),
                                                         fields[i]['name'], fields[i]['levels'], fields[i]['category'], i))
            keys.append((u, len(rows), len(hits)))
            rows += [point_v5.row(fields[i]['levels'], i, fields[i]['category'], fields[i]['subcategory'], langs[i])
                     for i in hits]
        return [(u, s, n) for u, (_, s, n) in zip(addr_links.shortest_keys([k[0] for k in keys]), keys)], rows

    out = type('Content', (), {})()
    out.prefix, out.df = old.prefix, old.df
    out.keys, out.rows = full_keys(lambda f: True)
    out.names, out.infos = names, infos
    cats = sorted({f['category'] for f in fields})
    out.cats = [((old.area, c),) + tuple(full_keys(lambda f, c=c: f['category'] == c)) for c in cats]
    subs = sorted({(f['category'], f['subcategory']) for f in fields if f['subcategory'] != 0xffff})
    out.subs = [((old.area,) + cs,) + tuple(full_keys(lambda f, cs=cs: (f['category'], f['subcategory']) == cs))
                for cs in subs]
    out.near = []
    for c in cats:
        seen, items = set(), []
        for i, f in enumerate(fields):
            k = (f['name'], f['lat'], f['lon'], f['xw'], f['y'])
            if f['category'] != c or k in seen:
                continue
            seen.add(k)
            items.append((i, c, f['subcategory'], f['lat'], f['lon'], f['xw'], f['y'], langs[i], point_v5.text(f['name'])))
        w = (0,) * 11 + (c,)
        out.near.append((w, items, any(it[2] != 0xffff for it in items)))
    out.adm_head, out.adm_df = old.adm_head, old.adm_df
    out.adm_keys, out.adm_rows, out.adm_names = old.adm_keys, old.adm_rows, old.adm_names
    st['phones'] = len(infos)
    return out, st, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--profile', required=True)
    ap.add_argument('--card', required=True, help='the base card image; read for ICODE001.DAT and its POINT file')
    ap.add_argument('--pois', required=True, help="extract_pois.py's output")
    ap.add_argument('--tiles', nargs='+', required=True)
    a = ap.parse_args()
    P = clarion_profile.load(a.profile)
    fmt = P.fmt
    if not fmt.refer_v5:
        raise SystemExit('profile %s is format %s; this writes X08-02 POINT files only' % (P.name, fmt.name))
    with fatread.Fat32(a.card) as fs:
        ic, _ = card_file(fs, '/REFER001/ICODE001.DAT')
        ordinal = icode_io.iso_ordinal(ic, P.country)
        name = 'POINT%03d.DAT' % (ordinal + 1)
        factory, packed = card_file(fs, '/REFER001/' + name)
    old = point_v5.Point(factory)
    if old.area != ordinal:
        raise SystemExit('%s on the card is area %d, not country %d (ordinal %d)' % (name, old.area, P.country, ordinal))
    pois = json.load(open(a.pois))
    tile_recs = poi_point.road_tile_records(a.tiles)
    new, st, skipped = build(old, pois, tile_recs, fmt)
    out = point_v5.assemble(new, fmt.deflate)
    back = point_v5.Point(out)
    assert (back.keys, back.rows, back.names, back.infos) == (new.keys, new.rows, new.names, new.infos)

    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    (od / name).write_bytes(out)
    files = {'REFER001/' + name: name}
    if packed:
        import card_pack
        c, i = card_pack.wrap(out)
        (od / (name + '.cmp')).write_bytes(c)
        (od / (name + '.ind')).write_bytes(i)
        files = {'REFER001/%s.%s' % (name, x): '%s.%s' % (name, x) for x in ('cmp', 'ind')}
    (od / 'card.json').write_text(json.dumps(dict(files=files, index={'REFER001:' + name: len(out)}), indent=1))
    (od / 'point_skipped.json').write_text(json.dumps(skipped, ensure_ascii=False, indent=1))
    per = collections.Counter(point_v5.parse_name_record(r)['category'] for r in new.names)
    print('%s: ordinal %d; %s' % (name, ordinal, ', '.join('%s %d' % kv for kv in sorted(st.items()))))
    print('categories: %s' % ' '.join('%d:%d' % kv for kv in sorted(per.items())))
    print('%s %d B (factory %d B): %d keys, %d rows, %d records, %d categories, %d sub-categories%s'
          % (name, len(out), len(factory), len(new.keys), len(new.rows), len(new.names), len(new.cats), len(new.subs),
             ', packed' if packed else ''))


if __name__ == '__main__':
    main()
