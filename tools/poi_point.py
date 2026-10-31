#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add facilities from extract_pois.py to a POINT file.

    poi_point.py POINT001.DAT pois.json out.DAT [--road-tile TILE.DAT] [--road-tile TILE.DAT ...]
                 [--fold Þ=TH,þ=th,Ð=D,ð=d] [--adm-lang 20] [--settlements-yfirst]
                 [--ref-base 0x8000] [--lang 63] [--municipalities ICELAND.osm.pbf]
                 [--allow-placeholder-refs] [--allow-missing-ladder]

The file is taken apart with point_io.Point and put back with every search frame rebuilt:
the letter trie and data records (one per word of every name, flag 0x4000 on the later
words), the name frame, the ADM frame (slots 280 and 287, the settlements' municipality and
settlement rows) and the per-category frames (slot 15 nearby trees, slot 278 CATEGORY
tries) for every facility category present, the ones already in the file included. With
an empty pois.json the output is the input, byte for byte, which is the check this runs
on the ADM frame before touching anything.

Each facility's ladder comes from --municipalities, an OSM extract of admin_level=6
boundaries (load_municipalities): the municipality polygon containing the facility, mapped
to the POINT settlement row of the same name (ladder_leaf). Without --municipalities the
old nearest-settlement ladder stands instead, for callers with no OSM extract on hand - it
picks the wrong town 39% of the time on the factory data (REPORT-poifix.md) and is kept
only for that case. --settlements-yfirst reads the existing settlement positions swapped,
for a POINT file whose settlements were written y first (card 030 and earlier, leaf-is-map
addr/build_point.py / build_addr_area0.py before PATCH-point-xy). The position words are
its own level-1 parcel (x first, our own convention).

Refs are the start/end nodes of the street link nearest the facility in its own parcel's
road record (--road-tile, read with shape_index/fa_encode; never a factory tile), by
point-to-segment distance over each link's own polyline rather than nearest vertex
(nearest_link_refs), and the search position is snapped to that link's foot point - the
map icon, written elsewhere, keeps the OSM position. A parcel with no road record at all,
or no --road-tile given at all, keeps the old placeholder ref and is listed on stderr.
Either kind of placeholder, or a facility whose municipality has no POINT node, makes this
exit non-zero unless --allow-placeholder-refs / --allow-missing-ladder says to proceed
anyway - stderr, not silence either way, because no card we have shows what the factory
does there (facility_refs' docstring) and a silently wrong ladder is the bug this replaced.
No address, no telephone, a data id past the highest one in the file. --fold adds keyboard
foldings to addre_io.fold_key for the trie keys; --adm-lang is the language the ADM rows
carry (20 where the country's language is none of the 18 menu languages).
"""
import json
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addre_io, clarion_bg, frames, point_io, shape_index  # noqa: E402

PARCEL_DEG = (1 / 48, 1 / 32)    # level-1 parcel height and width in degrees
EARTH_M = 111320                 # metres per degree of latitude (osm_roads.py, house_encode.py, rd_build.py)


def parcel_metres(lat):
    """Metres per level-1 position unit (0-2047 inside a parcel) at this latitude: x is
    1/32 degree of longitude (addre_io.parcel_point: fx = (lon + 924) * 32), shrunk by
    cos(lat); y is 1/48 degree of latitude (fy = lat * 48), no shrink."""
    return (1 / 32) / 2048 * EARTH_M * math.cos(math.radians(lat)), (1 / 48) / 2048 * EARTH_M


def keyunits(s, kw):
    out = bytearray()
    for ch in s:
        b = ch.encode('utf-8')
        assert len(b) <= kw, s
        out += b + b'\0' * (kw - len(b))
    return bytes(out)


# the factory keys (POINT018): '.' and '+' fold to a space (S.A.R.L. -> S A R L, P+R -> P R), quotes
# vanish, runs of spaces collapse; brackets stay, as the settlement builders keep them (GARDABAR (ALFTANES))
PUNCT = {'\xa0': ' ', '\u2013': ' ', '\u2014': ' ', '\u2018': "'", '\u2019': "'", '\u201c': '', '\u201d': '', '"': '',
         '.': ' ', '+': ' '}
unfolded = []     # characters no folding reached, dropped from the keys and reported


def word_starts(s):
    """Where the factory cuts a name into keys (POINT018): the whole name, every later word
    of two or more characters that starts with a letter or digit, and after a one-letter
    elision (D'ARMES gives ARMES, BAMBERG'S gives no S)."""
    out, p = [], 0
    for w in s.split(' '):
        if p == 0 or (len(w) >= 2 and w[0].isalnum()):
            out.append(p)
            if len(w) >= 4 and w[1] == "'":
                out.append(p + 2)
        p += len(w) + 1
    return out


def word_keys(name, kw, fold=None):
    """One key per word start, the whole remainder from it, and whether it is a later word."""
    s = ' '.join(addre_io.fold_key(''.join(PUNCT.get(c, c) for c in name), fold).upper().split())
    if not s.isascii():
        unfolded.extend(c for c in s if not c.isascii())
        s = ' '.join(''.join(c if c.isascii() else ' ' for c in s).split())
    return [(keyunits(s[p:], kw), p > 0) for p in word_starts(s)]


def parcel_latlon(la, lo, x, y):
    row, col = clarion_bg.parcel_rowcol(1, la, lo)
    return (row + y / 2048) * PARCEL_DEG[0], (col + x / 2048) * PARCEL_DEG[1] - 924


def road_tile_records(paths):
    """Parcel code -> (plain, bg) road records, merged from our own built level-1 tiles.
    shape_index.tile_records reads through clarion_tile.read_tile, fine for a tile we built
    ourselves; never call it on a factory tile (it reached 45 GB on one and swapped)."""
    recs = {}
    for path in paths:
        recs.update(shape_index.tile_records(open(path, 'rb').read()))
    return recs


def shape_links(shapes):
    """[(shape, lid, [q, ...])]: every link's own chain of point-words in geometry order,
    bounded by its own start (or, where it shares a node with the previous link rather
    than owning one, that link's last point)."""
    out = []
    for s in shapes:
        pts = sorted(s['points'].items())
        cur = None
        for i, (q, (role, lid)) in enumerate(pts):
            if lid is None:
                continue
            if cur is None or cur[1] != lid:
                prev = [pts[i - 1][0]] if i > 0 and role != 'start' else []
                cur = (s, lid, prev)
                out.append(cur)
            cur[2].append(q)
    return out


def seg_dist(px, py, ax, ay, bx, by):
    """Distance from (px, py) to segment a-b, and the foot of the perpendicular (clamped
    to the segment)."""
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    fx, fy = ax + t * dx, ay + t * dy
    return math.hypot(px - fx, py - fy), fx, fy


def nearest_link_refs(plain, bg, x, y, lat):
    """Refs and foot point of the street link nearest (x, y) in one level-1 0x00fa
    record: the link minimising point-to-segment distance (metres, isotropic at this
    latitude via parcel_metres) over its own polyline, not the nearest vertex over every
    shape - REPORT-poifix.md: 2,607/2,636 POINT018 facilities land on the factory's own
    referenced link this way, against 1,371/2,636 for nearest-vertex. Refs are that
    link's own start and end node (a node it shares with the previous link, rather than
    one it owns, stands in for a start node of its own) - REPORT-kanren.md's K and I,
    tagged 2/3 ascending or 4/5 descending."""
    _code, g, shapes = shape_index.shapes_of(plain, bg)
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    mx, my = parcel_metres(lat)
    px, py = x * mx, y * my
    best = None
    for shape, lid, qs in shape_links(shapes):
        for a, b in zip(qs, qs[1:]):
            ax, ay = (w[a] & 0x7ff) * mx, (w[a + 1] & 0x7ff) * my
            bx, by = (w[b] & 0x7ff) * mx, (w[b + 1] & 0x7ff) * my
            dist, fx, fy = seg_dist(px, py, ax, ay, bx, by)
            if best is None or dist < best[0]:
                best = (dist, shape, lid, fx / mx, fy / my)
    if best is None:
        return None
    _dist, shape, lid, fx, fy = best
    pts = sorted(shape['points'].items())
    idxs = [i for i, (pq, (prole, plid)) in enumerate(pts) if plid == lid]
    i0, i1 = idxs[0], idxs[-1]
    end_q, (end_role, _) = pts[i1]
    assert end_role == 'end', (lid, pts)
    start_role = pts[i0][1][0]
    assert start_role == 'start' or i0 > 0, (lid, pts)   # pts[i0 - 1] wraps to the last point otherwise
    start_q = pts[i0][0] if start_role == 'start' else pts[i0 - 1][0]
    start_v, end_v = start_q - g, end_q - g
    tags = (2, 3) if start_v < end_v else (4, 5)
    return ((tags[0], 0x40, start_v), (tags[1], 0x40, end_v)), (fx, fy)


def facility_refs(tile_recs, la, lo, x, y, lat, problems=None, name=''):
    """Refs and foot point for a facility: the nearest street link in its own level-1
    parcel. A parcel with no 0x00fa record at all has no factory precedent in
    POINT018/B48R060R.DAT to copy - every POINT018 facility matched to a German parcel in
    that tile has a road there; the few without a match are Luxembourg's, in a tile we do
    not have, not evidence of a roadless parcel - so this reports the gap and returns
    None; the caller keeps a placeholder ref (poi_point.merge: fails unless
    --allow-placeholder-refs) instead of inventing a fallback here."""
    rec = tile_recs.get((la, lo))
    result = nearest_link_refs(rec[0], rec[1], x, y, lat) if rec is not None else None
    if result is None and problems is not None:
        problems.append('%s: no street in its own parcel %04x %04x' % (name, la, lo))
    return result


def load_municipalities(pbf):
    """Administrative boundaries as (name, shapely polygon) pairs, for the ladder's
    point-in-polygon test: admin_level=6 relations (Iceland's municipalities), the way
    osm_roads.region reads a given relation list, but every one in the extract rather than
    a fixed set. Needs osmium and shapely; an OSM extract, never a factory tile."""
    import osmium
    import shapely
    import shapely.wkb
    fab = osmium.geom.WKBFactory()
    out = []

    class H(osmium.SimpleHandler):
        def area(self, a):
            if a.tags.get('boundary') != 'administrative' or a.tags.get('admin_level') != '6':
                return
            name = a.tags.get('name')
            if name:
                out.append((name, shapely.wkb.loads(fab.create_multipolygon(a), hex=True)))
    H().apply_file(pbf, locations=True)
    for _name, poly in out:
        shapely.prepare(poly)
    return out


def muni_name_of(lat, lon, municipalities):
    """The name of the municipality polygon containing (lat, lon), or None."""
    import shapely
    for name, poly in municipalities:
        if shapely.contains_xy(poly, lon, lat):
            return name
    return None


def town_municipalities(towns, municipalities):
    """Which OSM municipality each POINT settlement row's own position falls in: not a
    name match (an official admin name like "Hafnarfjarðarkaupstaður" or
    "Seltjarnarnesbær" doesn't fold onto the settlement's own display name,
    "HAFNARFJÖRÐUR" or "SELTJARNARNES" - Icelandic declension, not a simple affix), so
    this tests containment on each settlement's own coordinates instead and groups by
    that. Returns {index into towns: municipality name or None}."""
    return {i: muni_name_of(lat, lon, municipalities) for i, (_nm, _lv, (lat, lon)) in enumerate(towns)}


def ladder_leaf(muni_name, towns, town_munis, lat, lon):
    """The ladder of the POINT settlement(s) whose own position falls inside this
    municipality (town_munis, from town_municipalities). A municipality can hold more
    than one settlement row (GARÐABÆR and GARÐABÆR (ÁLFTANES), both inside today's merged
    Garðabær boundary) - among those, the nearest one to the facility is the leaf; with
    only one candidate there is no choice to make. None if the file has no row for this
    municipality at all."""
    cands = [towns[i][1:] for i, m in town_munis.items() if m == muni_name]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][0]
    return min(cands, key=lambda c: math.hypot(c[1][0] - lat, (c[1][1] - lon) * math.cos(math.radians(lat))))[0]


def adm_frame(at, p, fold, adm_lang):
    """The ADM frame for the settlements of a parsed POINT file, at file offset at."""
    names, keys = [], {}
    for nr in p.names:
        f = point_io.parse_name_record(nr)
        if f['category'] != point_io.SETTLEMENT:
            continue
        names += [(f['name'], f['levels'][:2], adm_lang), (f['name'], f['levels'], adm_lang)]
    for i, (nm, lv, lang) in enumerate(names):
        for u, later in word_keys(nm, frames.ADM_KW, fold):
            keys.setdefault(u, []).append((i, later))
    return frames.adm(at, p.area, p.nlevel, names, keys)


def merge(d, pois, fold=None, adm_lang=20, ref_base=0x8000, lang=point_io.LANG_LOCAL,
          road_tiles=None, settlements_yfirst=False, problems=None,
          allow_placeholder_refs=False, municipalities=None, allow_missing_ladder=False):
    """Bytes of the POINT file d with the facilities of pois added. road_tiles, paths to
    our own built level-1 tiles, gives every facility real refs (facility_refs) and snaps
    its search position to the foot of the chosen link (the factory's own facilities sit
    0-2 m from their referenced link, median 0, REPORT-poifix.md; the map icon keeps the
    OSM position, written elsewhere). Without road_tiles, or where a facility's own parcel
    has no road record, the old placeholder ref is kept and this raises unless
    --allow-placeholder-refs lets it through. settlements_yfirst reads the existing
    settlement records' position words swapped for the area ladder, for a POINT file
    written before PATCH-point-xy (card 030 and 032/033's bug: the ladder picked
    MOSFELLSBÆR for a hospital at (64.1410, -21.7590), 0.0444 against REYKJAVÍK's 0.0452, because the
    settlements decoded mirrored).

    municipalities, (name, polygon) pairs from load_municipalities, assigns the ladder by
    administrative containment instead of nearest settlement: the municipality polygon a
    facility's position falls in, mapped to the POINT settlement row of the same name (or
    the nearest of several sharing it as a prefix - ladder_leaf). Without municipalities
    the old nearest-settlement ladder stands, for callers with no OSM extract on hand.
    A facility whose municipality has no row in the file gets the country's own root ladder
    and this raises unless --allow-missing-ladder lets it through - never the nearest
    settlement regardless of distance.

    problems, a list, collects one line per facility with a placeholder ref or a missing
    ladder, if given, whether or not the corresponding --allow- flag was passed."""
    p = point_io.Point(d)
    old_adm, h, dfo = adm_frame(p.extra_off, p, fold, adm_lang)
    if d[p.extra_off:p.extra_off + len(old_adm)] != old_adm:
        raise SystemExit('the ADM frame rebuilt from the settlements differs from the file; '
                         'check --fold and --adm-lang')
    fields = [point_io.parse_name_record(nr) for nr in p.names]
    towns = [(f['name'], f['levels'],
              parcel_latlon(f['lat'], f['lon'], *((f['y'], f['x']) if settlements_yfirst else (f['x'], f['y']))))
             for f in fields if f['category'] == point_io.SETTLEMENT]
    root = (p.area,) + (0,) * (p.nlevel - 1)
    road_problems, ladder_problems = [], []
    town_munis = town_municipalities(towns, municipalities) if municipalities is not None else None

    def ladder(lat, lon, name):
        if municipalities is not None:
            muni = muni_name_of(lat, lon, municipalities)
            lv = ladder_leaf(muni, towns, town_munis, lat, lon) if muni else None
            if lv is not None:
                return lv
            msg = ('%s: outside every municipality polygon' % name if muni is None else
                   '%s: no POINT node for municipality %r' % (name, muni))
            ladder_problems.append(msg)
            if problems is not None:
                problems.append(msg)
            return root
        if not towns:
            return root
        return min(towns, key=lambda t: math.hypot(t[2][0] - lat, (t[2][1] - lon) * math.cos(math.radians(lat))))[1]

    tile_recs = road_tile_records(road_tiles) if road_tiles else None
    hits = {}        # key -> [(flags, name index, record bytes)]
    for k, start, n in p.keys:
        for i in range(start, start + n):
            w = struct.unpack('>20H', p.rec(i))
            hits.setdefault(k, []).append((w[6], w[15], p.rec(i)))
    next_id = 1 + max([struct.unpack_from('>I', p.rec(i), 32)[0] for i in range(p.nrec)
                       if struct.unpack_from('>I', p.rec(i), 32)[0] != 0xffffffff] + [0])
    names = list(p.names)
    for poi in pois:
        idx = len(names)
        la, lo, y, x = addre_io.parcel_point(poi['lat'], poi['lon'])
        lv = ladder(poi['lat'], poi['lon'], poi['name'])
        result = facility_refs(tile_recs, la, lo, x, y, poi['lat'], problems, poi['name']) if tile_recs is not None else None
        if result is None:
            road_problems.append(poi['name'])
            if tile_recs is None and problems is not None:
                problems.append('%s: no --road-tile given' % poi['name'])
            ref = ref_base + 4 * idx
            refs = ((2, 0x40, ref), (3, 0x40, ref + 1))
        else:
            refs, (fx, fy) = result
            x, y = max(0, min(2047, round(fx))), max(0, min(2047, round(fy)))
        names.append(point_io.name_record(poi['name'], lv, p.nlevel, poi['cat'], poi['sub'], lat=la, lon=lo, x=x, y=y,
                                          refs=refs))
        for u, later in word_keys(poi['name'], p.kw, fold):
            fl = 0x4000 if later else 0
            hits.setdefault(u, []).append((fl, idx, point_io.data_record(
                lv, p.nlevel, idx, poi['cat'], poi['sub'], lat=la, lon=lo, x=x, y=y, lang=lang, flags=fl,
                data_id=next_id)))
        next_id += 1
    if road_problems and not allow_placeholder_refs:
        raise SystemExit('%d facilit%s with a placeholder ref (no road tile, or none in its own parcel); '
                         'pass --allow-placeholder-refs to proceed:\n  %s'
                         % (len(road_problems), 'y' if len(road_problems) == 1 else 'ies',
                            '\n  '.join(road_problems[:20] + (['...'] if len(road_problems) > 20 else []))))
    if ladder_problems and not allow_missing_ladder:
        raise SystemExit('%d facilit%s with no POINT node for their municipality; '
                         'pass --allow-missing-ladder to proceed:\n  %s'
                         % (len(ladder_problems), 'y' if len(ladder_problems) == 1 else 'ies',
                            '\n  '.join(ladder_problems[:20] + (['...'] if len(ladder_problems) > 20 else []))))
    keys, recs = [], []
    for u in sorted(hits):
        hs = sorted(hits[u], key=lambda h: h[:2])
        keys.append((u, len(recs), len(hs)))
        recs += [h[2] for h in hs]
    # the +0x3c hint stays what the file has: it covers the longest refs+items field, which a
    # facility (two refs, no items: 6 halfwords) never raises above the settlements'
    for nr in names[len(p.names):]:
        assert (nr[10], nr[11]) == (6, 0), (nr[10], nr[11])
    assert frames.point_d(names) == frames.point_d(p.names), 'a facility record raised the +0x3c hint'
    out, _nb, _tb = point_io.assemble(p.prefix, p.df, keys, recs, names, list(p.tels), p.cr, p.kw, p.area, p.nlevel)
    at = frames.up(len(out))
    q = point_io.Point(out)
    frame, h, dfo = adm_frame(at, q, fold, adm_lang)
    out = bytearray(out) + b'\0' * (at - len(out)) + frame
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
    # the category frames: every facility category and (category, sub-category) pair present,
    # trie keys in key order, records in the factory's within-key order
    cs_of = {i: (f['category'], f['subcategory']) for i, f in
             ((i, point_io.parse_name_record(nr)) for i, nr in enumerate(names))}

    def by(pred):
        return point_io.select_records(keys, recs, names, lambda r: pred(cs_of[struct.unpack_from('>H', r, 30)[0]]), fold)
    cats = [(code,) + by(lambda cs: cs[0] == code)
            for code in sorted({c for c, s in cs_of.values() if c != point_io.SETTLEMENT})]
    subs = [pair + by(lambda cs: cs == pair)
            for pair in sorted({cs for cs in cs_of.values() if cs[0] != point_io.SETTLEMENT and cs[1] != 0xffff})]
    if cats:
        at = frames.up(len(out))
        blob, s15, s278 = point_io.category_frames(at, p.area, cats, names, fold)
        out += b'\0' * (at - len(out)) + blob
        struct.pack_into('>IH', out, 0x800 + 6 * 15, *s15)
        struct.pack_into('>IH', out, 0x800 + 6 * 278, *s278)
    if subs:
        at = frames.up(len(out))
        blob, s279 = point_io.subcategory_frames(at, p.area, subs, p.kw)
        out += b'\0' * (at - len(out)) + blob
        struct.pack_into('>IH', out, 0x800 + 6 * 279, *s279)
    return bytes(out), cats


def main(argv):
    def opt(name, default):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default
    fold = opt('--fold', None)
    fold = dict(kv.split('=') for kv in fold.split(',')) if fold else None
    adm_lang = int(opt('--adm-lang', '20'))
    ref_base = int(opt('--ref-base', '0x8000'), 0)
    lang = int(opt('--lang', str(point_io.LANG_LOCAL)))
    settlements_yfirst = '--settlements-yfirst' in argv
    if settlements_yfirst:
        argv.remove('--settlements-yfirst')
    allow_placeholder_refs = '--allow-placeholder-refs' in argv
    if allow_placeholder_refs:
        argv.remove('--allow-placeholder-refs')
    allow_missing_ladder = '--allow-missing-ladder' in argv
    if allow_missing_ladder:
        argv.remove('--allow-missing-ladder')
    municipalities_pbf = opt('--municipalities', None)
    road_tiles = []
    while '--road-tile' in argv:
        i = argv.index('--road-tile')
        road_tiles.append(argv[i + 1])
        del argv[i:i + 2]
    src, pois_path, dest = argv
    d = open(src, 'rb').read()
    pois = json.load(open(pois_path))
    municipalities = load_municipalities(municipalities_pbf) if municipalities_pbf else None
    problems = []
    out, cats = merge(d, pois, fold, adm_lang, ref_base, lang, road_tiles or None, settlements_yfirst, problems,
                      allow_placeholder_refs, municipalities, allow_missing_ladder)
    open(dest, 'wb').write(out)
    p, q = point_io.Point(d), point_io.Point(out)
    print('%s: names %d -> %d, records %d -> %d, %d -> %d bytes; categories %s'
          % (dest, len(p.names), len(q.names), p.nrec, q.nrec, len(d), len(out),
             ' '.join('%d:%d' % (code, len({struct.unpack_from(">H", r, 30)[0] for r in cr})) for code, ck, cr in cats)))
    if unfolded:
        print('  %d characters no folding reached, dropped from the keys: %s' % (len(unfolded), ''.join(sorted(set(unfolded)))))
    if problems:
        print('  %d problems (placeholder ref or missing ladder):' % len(problems))
        for line in problems:
            print('    ' + line)


if __name__ == '__main__':
    main(sys.argv[1:])
