#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Facility icons and POINT search records against POI-draft.md: the position words, the
mixed 8/10-byte layer-25 group, the multi-parcel nearby tree, the extractor's tag rules and
the poi_point merge.

    python3 -m unittest tests.test_pois -v

The factory cases need OSM2CLARION_FACTORY_TILE (B48R060R.DAT, the tile the draft measured)
and OSM2CLARION_FACTORY_POINT (REFER001/POINT018.DAT inflated, Luxembourg) or they skip.
Nothing from either is written anywhere.
"""
import os, struct, sys, unittest, unittest.mock, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import addre_io, clarion_bg, extract_pois, frames, point_io, poi_point  # noqa: E402
from tile_scan import TileScan  # noqa: E402

TILE_ENV, POINT_ENV = 'OSM2CLARION_FACTORY_TILE', 'OSM2CLARION_FACTORY_POINT'


def factory_point():
    path = os.environ.get(POINT_ENV)
    if not path or not os.path.exists(path):
        raise unittest.SkipTest('set %s to an inflated POINT018.DAT' % POINT_ENV)
    return open(path, 'rb').read()


def factory_record(level, code):
    """The inflated section-1 background record with this parcel code, read off the tile
    one 2 KB prefix at a time (never read_tile)."""
    path = os.environ.get(TILE_ENV)
    if not path or not os.path.exists(path):
        raise unittest.SkipTest('set %s to a factory tile' % TILE_ENV)
    with TileScan(path) as t:
        for r in t.records(section=1, level=level):
            if not r.is_zlib or r.n > (4 << 20):
                continue
            head = zlib.decompressobj().decompress(bytes(t.m[r.off:r.off + 2048]))[:6]
            if len(head) == 6 and struct.unpack('>3H', head) == code + (0x19,):
                return r.plain()
    raise unittest.SkipTest('record %04x %04x not in the tile' % code)


def latlon(la, lo, x, y):
    row, col = clarion_bg.parcel_rowcol(1, la, lo)
    return (row + y / 2048) / 48, (col + x / 2048) / 32 - 924


def km(a, b):
    import math
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(math.radians(a[0])))


class TestPositionWords(unittest.TestCase):
    """POI-draft.md 2.1-2.3: the two words after the parcel code are 0x800 | x, then y."""

    def test_data_record_writes_x_first(self):
        r = point_io.data_record((17, 2, 2, 3, 10), 5, 1564, 5, 3524, lat=0xa53c, lon=0xd11c, x=457, y=1773)
        self.assertEqual(r[18:26], bytes.fromhex('a53cd11c09c906ed'), 'CARLTON, POINT018 record 2747')
        f = point_io.parse_data_record(r)
        self.assertEqual((f['x'], f['y'], f['category'], f['subcategory']), (457, 1773, 5, 3524))

    def test_name_record_round_trip(self):
        nr = point_io.name_record('CLINIQUE SAINTE-THÉRÈSE ZITHA', (17, 2, 2, 3, 10), 5, 11, lat=0xa53c, lon=0xd11c,
                                  x=300, y=2018, address='RUE SAINTE-ZITHE', house='36',
                                  items=bytes.fromhex('075400003a34393a3b3a33393a00'),
                                  refs=((2, 0x40, 1506), (3, 0x40, 1513)), tel_idx=1523)
        self.assertEqual(nr[2:10], bytes.fromhex('a53cd11c092c07e2'))
        f = point_io.parse_name_record(nr)
        self.assertEqual((f['x'], f['y'], f['house'], f['address'], f['tel_idx']), (300, 2018, '36', 'RUE SAINTE-ZITHE', 1523))
        self.assertEqual(point_io.name_record(**f), nr)

    def test_factory_data_records_round_trip(self):
        d = factory_point()
        p = point_io.Point(d)
        for i, x, y in ((2747, 457, 1773), (3385, 300, 2018), (150, 1515, 1920)):   # CARLTON, Zitha, Aire de Berchem
            f = point_io.parse_data_record(p.rec(i))
            self.assertEqual((f['x'], f['y']), (x, y))
            self.assertEqual(point_io.data_record(f['levels'][:p.nlevel], p.nlevel, f['name_idx'], f['category'],
                                                  f['subcategory'], f['lat'], f['lon'], f['x'], f['y'], f['lang'],
                                                  f['flags'], f['data_id']), p.rec(i))

    def test_factory_name_records_round_trip(self):
        p = point_io.Point(factory_point())
        for nr in p.names:
            self.assertEqual(point_io.name_record(**point_io.parse_name_record(nr)), nr)

    def test_factory_settlements_read_x_first(self):
        """Settlement records use the same words: read x first, Luxembourg's towns land on
        their centres; read y first they are a kilometre off."""
        p = point_io.Point(factory_point())
        centres = {'GREVENMACHER': (49.6806, 6.4414), 'REMICH': (49.5450, 6.3669), 'VIANDEN': (49.9347, 6.2061),
                   'ECHTERNACH': (49.8117, 6.4217)}
        seen = 0
        for nr in p.names:
            f = point_io.parse_name_record(nr)
            if f['category'] != point_io.SETTLEMENT or f['name'] not in centres:
                continue
            seen += 1
            self.assertLess(km(latlon(f['lat'], f['lon'], f['x'], f['y']), centres[f['name']]), 0.4, f['name'])
            self.assertGreater(km(latlon(f['lat'], f['lon'], f['y'], f['x']), centres[f['name']]), 0.9, f['name'])
        self.assertEqual(seen, len(centres))


class TestNearbyTree(unittest.TestCase):
    """POI-draft.md 2.4: the slot-15 frame with many parcels per category."""

    def items(self):
        return [(10, 11, 0xffff, 0xb01e, 0xc31c, 0x800 | 1000, 500, 63, 'A'),
                (11, 11, 0xffff, 0xb01e, 0xc31c, 0x800 | 1000, 500, 63, 'B'),
                (12, 11, 0xffff, 0xb01e, 0xc31d, 0x800 | 7, 9, 63, 'C'),
                (13, 11, 0xffff, 0xb000, 0xc380, 0x800 | 1, 2, 63, 'D')]

    def test_layout(self):
        b = point_io.nearby_frame(self.items())
        # level 1: (b000, c300) and (b000, c380) ... the two level-1 keys cover la & 0xff80, lo & 0xff80
        l1 = [struct.unpack_from('>HIHHHHH', b, 16 * i) for i in range(2)]
        self.assertEqual([(e[3], e[4]) for e in l1], [(0xb000, 0xc300), (0xb000, 0xc380)])
        self.assertEqual(struct.unpack_from('>HHHI', b, 32), (5, 0xffff, 0xffff, 0xffffffff))
        self.assertEqual(l1[0][1], 0x80, 'the first child block follows at +0x100, in halfwords')
        self.assertEqual(l1[0][2], 8 * 1 + 5, 'and holds one entry')
        # depth first: L2, L3 and the two leaves of the first subtree, then the second subtree
        l3 = [struct.unpack_from('>HIHHHHH', b, 0x200 + 16 * i) for i in range(2)]
        self.assertEqual([(e[3], e[4], e[1], e[2]) for e in l3], [(0xb01e, 0xc31c, 0x180, 8 * 2 + 5), (0xb01e, 0xc31d, 0x200, 8 + 5)])
        self.assertEqual(struct.unpack_from('>HIHHHHH', b, 0x300), (8, 10, 0, 0, 0x0011, 0x8000 | 0x800 | 1000, 0x1f << 11 | 500))
        self.assertEqual(struct.unpack_from('>HHHI', b, 0x300 + 32), (5, 0xffff, 0xffff, 0x200 // 2), 'the leaf points back at its L3')
        self.assertEqual(len(b), 8 * 256)
        items, sub = point_io.parse_nearby(b, 0, 2)
        self.assertFalse(sub)
        self.assertEqual(sorted(it[:8] for it in items), sorted(it[:8] for it in self.items()))

    def test_sub_category_level(self):
        its = [(1, 5, 3524, 0xb01e, 0xc31c, 0x800 | 3, 4, 63, 'CARLTON'), (2, 5, 3500, 0xb01e, 0xc31c, 0x800 | 3, 4, 63, 'INN')]
        b = point_io.nearby_frame(its, sub=True)
        l4 = struct.unpack_from('>HIHHHHH', b, 0x300)
        self.assertEqual(l4[3:5], (0, 5), 'one entry keyed (0, category) under the parcel')
        leaf = [struct.unpack_from('>HIHHHHH', b, 0x400 + 16 * i) for i in range(2)]
        self.assertEqual([e[4] for e in leaf], [0x3500, 0x3524], 'items carry the sub-category as BCD, sorted')
        items, sub = point_io.parse_nearby(b, 0, 1)
        self.assertTrue(sub)
        self.assertEqual(sorted(it[:8] for it in items), sorted(it[:8] for it in its))

    def test_leaf_order_collates_accents(self):
        self.assertLess(point_io.collate('NÁRODNÉ'), point_io.collate('NEMZETI'))
        self.assertGreater(point_io.collate('LÜKSEMBURG'), point_io.collate('LUXENBORG'))

    def test_factory_frames_round_trip(self):
        d = factory_point()
        p = point_io.Point(d)
        a, n = struct.unpack_from('>IH', d, 0x800 + 6 * 15)
        t15 = addre_io.unaddr(a)
        codes = []
        for e in range(1000):
            w = struct.unpack_from('>HIIHHHHHHHHH', d, t15 + 28 * e)
            base, size, nl1 = addre_io.unaddr(w[1]), 2 * w[2], (w[5] - 5) // 8
            items, sub = point_io.parse_nearby(d, base, nl1, p.names)
            self.assertEqual(point_io.nearby_frame(items, sub), d[base:base + size], 'category %d' % w[11])
            self.assertEqual(w[6:11], (0x8000, 8, e, 0, 1))
            codes.append(w[11])
            if w[0] == 0xffff:
                break
        self.assertEqual(len(codes), 56)
        self.assertTrue({5, 6, 11, 30} <= set(codes))

    def test_category_frames_table(self):
        names = [point_io.name_record('ZITHA', (0, 0, 0), 3, 11, lat=0xb01e, lon=0xc31c, x=300, y=2018)]
        recs = [point_io.data_record((0, 0, 0), 3, 0, 11, lat=0xb01e, lon=0xc31c, x=300, y=2018)]
        blob, s15, s278 = point_io.category_frames(0x3300, 0, [(11, [(b'Z\0I\0', 0, 1)], recs)], names)
        w = struct.unpack_from('>HIIHHHHHHHHH', blob, 0)
        self.assertEqual(w[0], 0xffff)
        self.assertEqual(addre_io.unaddr(w[1]), 0x3400, 'the nearby block follows the one-entry table')
        self.assertEqual(w[2], 4 * 256 // 2, 'four blocks: three levels and a leaf')
        self.assertEqual(w[5:], (8 * 1 + 5, 0x8000, 8, 0, 0, 1, 11))
        items, sub = point_io.parse_nearby(blob, 0x100, 1, names)
        self.assertEqual(items[0][:8], (0, 11, 0xffff, 0xb01e, 0xc31c, 0x800 | 300, 2018, 63))


class TestIconGroups(unittest.TestCase):
    """POI-draft.md 3: layer-25 points of 8 and 10 bytes in one sub-group, and the side section."""

    def check_icons(self, plain, npoints, nicons):
        rec = clarion_bg.parse_record(plain)
        g = rec['layers'][25]
        self.assertNotIn('raw', g, g.get('note'))
        shapes = [s for sub in [g] + g.get('more', []) for s in sub['shapes']]
        self.assertEqual(len(shapes), npoints)
        icons = [s for s in shapes if s['type'] == clarion_bg.ICON]
        self.assertEqual(len(icons), nicons)
        side = rec['sections']['points']
        for s in icons:
            self.assertEqual(s['idx'] % 6, 0)
            e = struct.unpack_from('>6H', side, 2 * s['idx'])
            self.assertEqual(e[:4] + (e[4] & 0xf000, e[5]), (0, 0, 0xffff, 0, 0x2000, 0))
        self.assertEqual(clarion_bg.build_record(rec), plain)
        return rec, icons, side

    def test_factory_level1_record(self):
        plain = factory_record(1, (0xa53c, 0xd11c))        # Luxembourg Gare
        rec, icons, side = self.check_icons(plain, 178, 166)
        self.assertEqual(len(side), 166 * 12)
        zitha = [s for s in icons if s['pts'][0] == (288, 2002)]
        self.assertEqual(struct.unpack_from('>6H', side, 2 * zitha[0]['idx'])[4], 0x2000 | 2000)

    def test_factory_level2_record(self):
        plain = factory_record(2, (0xa53f, 0xd112))
        rec, icons, side = self.check_icons(plain, 553, 521)
        self.assertEqual(len(rec['layers'][25]['shapes']), 553, 'one sub-group counts past 255: the count word is 16 bits')

    def test_synthetic_record_round_trip(self):
        pts = [(100, 200, 2000), (1500, 300, 3272), (7, 2047, 907), (7, 2047, 3524)]
        labels = [dict(type=0x806c, ref=0xffff, pts=[(50, 60)])]
        w = [0] * 22
        w[0], w[1] = clarion_bg.parcel_code(1, 3078, 28866)
        w[2] = 0x19
        w[7], w[8] = 0xffff, 0
        rec = dict(hdr=w, nlayers=26, layers={25: clarion_bg.icon_layer(pts, labels)},
                   sections={'points': clarion_bg.icon_side(pts)})
        plain = clarion_bg.build_record(rec)
        back, icons, side = self.check_icons(plain, 5, 4)
        self.assertEqual([struct.unpack_from('>6H', side, 2 * s['idx'])[4] & 0xfff for s in icons], [2000, 3272, 907, 3524])
        self.assertEqual(back['layers'][25]['shapes'][0], labels[0], 'the typed label keeps its 8-byte form')
        self.assertEqual(struct.unpack_from('>22H', plain, 0)[15:17], (len(plain) // 4 - 48 // 4, 4 * 12 // 4))

    def test_count_word_is_16_bits(self):
        g = dict(kind=0, shapes=[dict(type=0x100c, ref=0xffff, pts=[(i, i)]) for i in range(300)])
        raw = clarion_bg.build_group(g)
        self.assertEqual(struct.unpack_from('>HHH', raw, 0), (1, 3, 300))
        back = clarion_bg.parse_group(raw, 0, len(raw))
        self.assertEqual(len(back['shapes']), 300)
        self.assertEqual(clarion_bg.build_group(dict(kind=0x80, shapes=[]))[4:6], b'\x80\x00')


class TestParcels(unittest.TestCase):
    def test_parcel_of_matches_parcel_point(self):
        for lat, lon in ((64.1466, -21.9426), (63.4, -24.53), (66.65, -13.41), (49.6, 6.13)):
            row, col, x, y = clarion_bg.parcel_of(1, lat, lon)
            la, lo, py, px = addre_io.parcel_point(lat, lon)
            self.assertEqual((la, lo, px, py), clarion_bg.parcel_code(1, row, col) + (x, y))
            self.assertEqual(clarion_bg.parcel_rowcol(1, la, lo), (row, col))
            r2, c2, x2, y2 = clarion_bg.parcel_of(2, lat, lon)
            X, Y = col * 2048 + x, row * 2048 + y
            self.assertEqual((r2, c2, x2, y2), (Y // 8192, X // 8192, (X // 4) % 2048, (Y // 4) % 2048),
                             'the level-2 position follows from the level-1 words')
            self.assertEqual(clarion_bg.parcel_rowcol(2, *clarion_bg.parcel_code(2, r2, c2)), (r2, c2))


class TestExtract(unittest.TestCase):
    def test_classify(self):
        c = extract_pois.classify
        self.assertEqual(c({'amenity': 'restaurant', 'cuisine': 'pizza;italian'}), (6, 910))
        self.assertEqual(c({'amenity': 'restaurant', 'cuisine': 'icelandic'}), (6, 924))
        self.assertEqual(c({'amenity': 'fast_food', 'cuisine': 'burger'}), (6, 906))
        self.assertEqual(c({'amenity': 'cafe'}), (6, 924))
        self.assertEqual(c({'tourism': 'hotel', 'brand': 'Hilton'}), (5, 3511))
        self.assertEqual(c({'tourism': 'hotel', 'brand': 'Radisson Blu'}), (5, 3520))
        self.assertEqual(c({'tourism': 'guest_house'}), (5, 3500))
        self.assertEqual(c({'amenity': 'hospital'}), (11, 0xffff))
        self.assertEqual(c({'healthcare': 'hospital'}), (11, 0xffff))
        self.assertEqual(c({'highway': 'rest_area'}), (30, 0xffff))
        self.assertIsNone(c({'amenity': 'fuel'}))
        self.assertIsNone(c({'tourism': 'camp_site'}))


class TestPoiPoint(unittest.TestCase):
    """poi_point.merge on a POINT file built here: nothing added gives the file back, and
    facilities come out with x-first positions, their nearest town's ladder and frames."""

    TOWNS = [('REYKJAVÍK', (0, 0, 0), 64.1466, -21.9426), ('AKUREYRI', (0, 3, 0), 65.6835, -18.1002)]
    FOLD = {'Þ': 'TH', 'þ': 'th', 'Ð': 'D', 'ð': 'd'}

    def base_file(self):
        kw, cr = 2, 14
        names, hits = [], {}
        for i, (name, lv, lat, lon) in enumerate(self.TOWNS):
            la, lo, y, x = addre_io.parcel_point(lat, lon)
            names.append(point_io.name_record(name, lv, 3, point_io.SETTLEMENT, lat=la, lon=lo, x=x, y=y,
                                              refs=((2, 0x40, 0x8000 + 4 * i), (3, 0x40, 0x8001 + 4 * i))))
            for u, later in poi_point.word_keys(name, kw, self.FOLD):
                hits.setdefault(u, []).append(point_io.data_record(lv, 3, i, point_io.SETTLEMENT, lat=la, lon=lo, x=x, y=y,
                                                                   flags=0x4000 if later else 0))
        keys, recs = [], []
        for u in sorted(hits):
            keys.append((u, len(recs), len(hits[u])))
            recs += hits[u]
        out, _, _ = point_io.assemble(frames.point_prefix((4800, 1280, -1680, 3600), '14/11/17/01'), bytes(frames.POINT_DF),
                                      keys, recs, names, [], cr, kw, 0, 3, maxkey_hint=frames.point_d(names))
        at = frames.up(len(out))
        frame, h, dfo = poi_point.adm_frame(at, point_io.Point(out), self.FOLD, 20)
        out = bytearray(out) + b'\0' * (at - len(out)) + frame
        struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
        struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
        return bytes(out)

    def test_nothing_added_is_the_same_file(self):
        d = self.base_file()
        out, cats = poi_point.merge(d, [], self.FOLD)
        self.assertEqual(out, d)
        self.assertEqual(cats, [])

    def test_facilities(self):
        d = self.base_file()
        pois = [dict(name='Landspítali', cat=11, sub=0xffff, lat=64.1340, lon=-21.9260),
                dict(name='Hótel Kea', cat=5, sub=3500, lat=65.6815, lon=-18.0900),
                dict(name='Þrír Frakkar', cat=6, sub=924, lat=64.1440, lon=-21.9300)]
        out, cats = poi_point.merge(d, pois, self.FOLD)
        q = point_io.Point(out)
        self.assertEqual(len(q.names), 5)
        self.assertEqual([c for c, _, _ in cats], [5, 6, 11])
        f = point_io.parse_name_record(q.names[2])
        la, lo, y, x = addre_io.parcel_point(64.1340, -21.9260)
        self.assertEqual((f['name'], f['category'], f['lat'], f['lon'], f['x'], f['y'], f['levels']),
                         ('Landspítali', 11, la, lo, x, y, (0, 0, 0)))
        self.assertEqual(point_io.parse_name_record(q.names[3])['levels'], (0, 3, 0), 'Akureyri is the nearest town')
        recs = [point_io.parse_data_record(q.rec(i)) for i in range(q.nrec)]
        mine = [r for r in recs if r['name_idx'] == 4]
        self.assertEqual(sorted(r['flags'] for r in mine), [0, 0x4000], 'one record per word, the later word flagged')
        self.assertEqual({(r['x'], r['y'], r['data_id']) for r in mine}, {addre_io.parcel_point(64.1440, -21.9300)[3:1:-1] + (3,)})
        # the frames: slot 15 names three categories whose nearby trees give the facilities back
        a, n = struct.unpack_from('>IH', out, 0x800 + 6 * 15)
        t15 = addre_io.unaddr(a)
        for e, code in enumerate((5, 6, 11)):
            w = struct.unpack_from('>HIIHHHHHHHHH', out, t15 + 28 * e)
            self.assertEqual(w[11], code)
            items, sub = point_io.parse_nearby(out, addre_io.unaddr(w[1]), (w[5] - 5) // 8, q.names)
            self.assertEqual(sub, code != 11)
            self.assertEqual([it[1] for it in items], [code])
        frame = poi_point.adm_frame(q.extra_off, q, self.FOLD, 20)[0]
        self.assertEqual(out[q.extra_off:q.extra_off + len(frame)], frame, 'the ADM frame is rebuilt where the tails end')
        # slot 279: one SUB-CATEGORY entry per (category, sub) pair, its run the facilities' records
        table = point_io.category_table(out, 279)
        self.assertEqual([e['tail'] for e in table], [(0, 0xffff, 5, 3500), (0, 0xffff, 6, 924)])
        self.assertEqual([(e['cr'], e['kw'], e['nrec']) for e in table], [(12, 1, 2), (12, 1, 2)])
        keys = point_io.trie_keys(out, table[1]['trie_at'], 12, 1)
        self.assertEqual([k for k, s, n in keys], [b'FRAKKAR', b'THRIR FRAKKAR'], 'one-byte key units, the fold applied')
        self.assertEqual({point_io.parse_data_record(out[table[1]['rec_at'] + 40 * i:][:40])['name_idx'] for i in range(2)}, {4})
    def test_hint_guard(self):
        """A facility record with a third ref would raise the +0x3c hint merge does not recompute."""
        d = self.base_file()
        real = point_io.name_record

        def three_refs(*a, **kw):
            kw['refs'] = kw['refs'] + ((2, 0x40, 1),)
            return real(*a, **kw)
        with unittest.mock.patch.object(poi_point.point_io, 'name_record', three_refs):
            with self.assertRaises(AssertionError):
                poi_point.merge(d, [dict(name='Hótel Kea', cat=5, sub=3500, lat=65.6815, lon=-18.0900)], self.FOLD)

    def test_word_keys_cut_like_the_factory(self):
        def keys(name):
            return [u.replace(b'\0', b'').decode() for u, later in poi_point.word_keys(name, 2, self.FOLD)]
        self.assertEqual(keys("PLACE D'ARMES"), ["PLACE D'ARMES", "D'ARMES", 'ARMES'])
        self.assertEqual(keys("BAMBERG'S"), ["BAMBERG'S"])
        self.assertEqual(keys('AUTOPOLIS S.A.'), ['AUTOPOLIS S A'])
        self.assertEqual(keys('4 SAISONS'), ['4 SAISONS', 'SAISONS'])
        self.assertEqual(keys('GARÐABÆR (ÁLFTANES)'), ['GARDABAR (ALFTANES)'], 'as the settlement builders keyed card 030')
        self.assertEqual(keys('ADRIANO & CO'), ['ADRIANO & CO', 'CO'])
        self.assertEqual(keys('Daddi’s Pizza'), ["DADDI'S PIZZA", 'PIZZA'])


class TestSubCategory(unittest.TestCase):
    """Slot 279, the SUB-CATEGORY table: POI-draft.md's missing frame, decoded off POINT018."""

    def test_entry_layout(self):
        e = point_io.subcategory_entry(1, 17, 2, 403, 0x697400, 0x18, 0xc, 0x714800, 5)
        self.assertEqual(len(e), 78)
        self.assertEqual(struct.unpack_from('>H', e, 0)[0], 78, 'next entry, in halfwords from the table start')
        self.assertEqual(struct.unpack_from('>H', e, 0x30)[0], 39)
        self.assertEqual((e[0x32], e[0x33]), (6, 1), 'cr 12, one-byte key units')
        self.assertEqual(struct.unpack_from('>HHH', e, 0x34), (1, 0x0c00, 4))
        self.assertEqual(e[0x3a:0x46], b'SUB-CATEGORY')
        self.assertEqual(struct.unpack_from('>HHHH', e, 0x46), (17, 0xffff, 2, 403))
        self.assertEqual(struct.unpack_from('>H', point_io.subcategory_entry(0, 17, 2, 403, 0, 0, 0, 0, 0, last=True), 0)[0], 0xffff)

    def test_record_order(self):
        """Inside one key: whole-name hits first, then the name, the ladder, the position."""
        names = [point_io.name_record(n, (0, 0, 0), 3, 6, 924) for n in ('ROMA', 'ANTICA ROMA', 'ROMA')]
        r = [point_io.data_record((0, 0, 0), 3, 0, 6, 924, lat=5, lon=5, flags=0),
             point_io.data_record((0, 0, 0), 3, 1, 6, 924, lat=1, lon=1, flags=0x4000),
             point_io.data_record((0, 0, 0), 3, 2, 6, 924, lat=4, lon=4, flags=0),
             point_io.data_record((0, 1, 0), 3, 2, 6, 924, lat=3, lon=3, flags=0)]
        self.assertEqual(point_io.record_order([r[1], r[3], r[0], r[2]], names), [r[2], r[0], r[3], r[1]])

    def test_factory_slot279_round_trip(self):
        d = factory_point()
        p = point_io.Point(d)
        recs = p.records()
        table = point_io.category_table(d, 279)
        self.assertEqual(len(table), 42)
        self.assertEqual(sorted({e['tail'][2] for e in table}), [2, 5, 6])
        subs = [e['tail'][2:] + point_io.select_records(p.keys, recs, p.names, lambda r, t=e['tail'][2:]: struct.unpack_from('>HH', r, 14) == t)
                for e in table]
        a, nb = struct.unpack_from('>IH', d, 0x800 + 6 * 279)
        at = addre_io.unaddr(a)
        blob, s279 = point_io.subcategory_frames(at, p.area, subs, p.kw)
        self.assertEqual(s279, (a, nb))
        self.assertEqual(blob, d[at:at + len(blob)], 'table, 42 tries and 42 record runs')
        self.assertEqual(len(blob), 712192)

    def test_factory_category_record_order(self):
        """The same within-key order gives 52 of the 56 CATEGORY runs (main order gives 35)."""
        d = factory_point()
        p = point_io.Point(d)
        recs = p.records()
        table = point_io.category_table(d, 278)
        self.assertEqual(len(table), 56)
        same = main = 0
        for e in table:
            fact = d[e['rec_at']:e['rec_at'] + 40 * e['nrec']]
            pred = lambda r, c=e['tail'][2]: struct.unpack_from('>H', r, 14)[0] == c
            _k, ordered = point_io.select_records(p.keys, recs, p.names, pred)
            same += b''.join(ordered) == fact
            main += b''.join(r for _u, s, n in p.keys for r in recs[s:s + n] if pred(r)) == fact
        self.assertEqual((same, main), (52, 35))


class TestCollation(unittest.TestCase):
    FOLD = {'Þ': 'TH', 'þ': 'th', 'Ð': 'D', 'ð': 'd'}

    def test_case_and_icelandic_letters(self):
        c = point_io.collate
        self.assertEqual(c('þórshöfn', self.FOLD), c('ÞÓRSHÖFN', self.FOLD))
        self.assertLess(c('reykjavík'), c('SELFOSS'), 'lower case does not sort after upper case')
        names = ['Ægissíða', 'Þórshöfn', 'Ðalvík', 'Tálknafjörður', 'Vík', 'Dalvík', 'Akureyri', 'Ísafjörður', 'Blönduós']
        self.assertEqual(sorted(names, key=lambda n: c(n, self.FOLD)),
                         ['Akureyri', 'Ægissíða', 'Blönduós', 'Dalvík', 'Ðalvík', 'Ísafjörður', 'Tálknafjörður', 'Þórshöfn', 'Vík'])
        self.assertEqual(c('Þ', self.FOLD), (('T', 'Þ'), ('H', '')))

    def test_leaf_sorted_case_insensitively(self):
        its = [(1, 11, 0xffff, 0xb01e, 0xc31c, 0x800 | 3, 4, 63, 'landspítali'), (2, 11, 0xffff, 0xb01e, 0xc31c, 0x800 | 3, 4, 63, 'KRISTNES')]
        b = point_io.nearby_frame(its, fold=self.FOLD)
        leaf = [struct.unpack_from('>HIHHHHH', b, 0x300 + 16 * i) for i in range(2)]
        self.assertEqual([e[1] for e in leaf], [2, 1])


class TestDedupe(unittest.TestCase):
    def poi(self, oid, name, lat, lon, cat=5, sub=3500):
        return dict(id=oid, name=name, cat=cat, sub=sub, lat=lat, lon=lon)

    def test_node_wins_over_area(self):
        node = self.poi('n6381120324', 'Lilja Guesthouse', 63.9000, -21.0000)
        area = self.poi('w742911004', 'Lilja Guesthouse', 63.90002, -21.00002)
        kept, dropped = extract_pois.dedupe([area, node])
        self.assertEqual(kept, [node])
        self.assertEqual(dropped, [(area, node)])

    def test_name_inside_the_other(self):
        a = self.poi('n1', 'Drangshlíð', 63.5, -19.5)
        b = self.poi('w2', 'Guesthouse Drangshlíð - Main building', 63.5002, -19.5003)
        self.assertEqual(extract_pois.dedupe([a, b])[0], [a])

    def test_kept_apart(self):
        a = self.poi('n1', 'Hótel Kea', 65.68, -18.09)
        far = self.poi('n2', 'Hótel Kea', 65.681, -18.09)                 # 111 m north
        other = self.poi('n3', 'Hótel Kea', 65.68, -18.09, cat=6, sub=924)   # a restaurant of the same name
        unrelated = self.poi('n4', 'Bautinn', 65.68001, -18.09001)
        self.assertEqual(len(extract_pois.dedupe([a, far, other, unrelated])[0]), 4)

    def test_clean_name(self):
        self.assertEqual(extract_pois.clean_name('Daddi’s  Pizza'), "Daddi's Pizza")
        self.assertEqual(extract_pois.clean_name('​Svarta Fjaran'), 'Svarta Fjaran')
        self.assertEqual(extract_pois.clean_name('Guesthouse 1×6'), 'Guesthouse 1x6')
        self.assertEqual(extract_pois.clean_name('Iceland´s Guesthouse'), "Iceland's Guesthouse")


class TestAddPoint(unittest.TestCase):
    """clarion_bg.add_point: the dealer and camera points of leaf-is-map join a record's icons."""

    def record(self):
        pts = [(100, 200, 2000), (1500, 300, 3272), (7, 2047, 907)]
        w = [0] * 22
        w[0], w[1] = clarion_bg.parcel_code(1, 3078, 28866)
        w[2] = 0x19
        w[7], w[8] = 0xffff, 0
        return dict(hdr=w, nlayers=26, layers={25: clarion_bg.icon_layer(pts)}, sections={'points': clarion_bg.icon_side(pts)})

    def test_dealer_and_camera_join_the_icons(self):
        rec = self.record()
        clarion_bg.add_point(rec, 0x806c, 348, 32)
        cam = clarion_bg.camera_service_record(52, 17, 1)
        s = clarion_bg.add_point(rec, 0x0050, 137, 412, side=cam)
        self.assertEqual(s['idx'], 3 * 12 // 2, 'the camera entry follows the three icon entries')
        back = clarion_bg.parse_record(clarion_bg.build_record(rec))
        shapes = back['layers'][25]['shapes']
        self.assertEqual([sh['type'] for sh in shapes], [0x806c, 0xff, 0xff, 0xff, 0x50])
        self.assertEqual([sh.get('idx') for sh in shapes], [None, 0, 6, 12, 18])
        side = back['sections']['points']
        self.assertEqual(len(side), 36 + 20)
        self.assertEqual([struct.unpack_from('>6H', side, 2 * i)[4] & 0xfff for i in (0, 6, 12)], [2000, 3272, 907])
        self.assertEqual(side[36:], cam)
        self.assertEqual(clarion_bg.build_record(back), clarion_bg.build_record(rec))

    def test_empty_record(self):
        w = [0] * 22
        w[2] = 0x19
        w[7], w[8] = 0xffff, 0
        rec = dict(hdr=w, nlayers=0, layers={}, sections={})
        clarion_bg.add_point(rec, 0x0050, 1, 2, side=clarion_bg.camera_service_record(20, 17, 2))
        back = clarion_bg.parse_record(clarion_bg.build_record(rec))
        self.assertEqual(back['nlayers'], 26)
        self.assertEqual(back['layers'][25]['shapes'][0], dict(type=0x50, ref=0xffff, pts=[(1, 2)], idx=0))

    def test_refuses_an_unparsed_layer(self):
        rec = self.record()
        rec['layers'][25] = dict(raw=b'\0' * 8)
        with self.assertRaises(AssertionError):
            clarion_bg.add_point(rec, 0x806c, 1, 1)


if __name__ == '__main__':
    unittest.main()
