#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""build_addr_country: the ladder from an ICODE area tree, the entries and their ranges."""
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))

import addr_links
import addr_resolve
import addre_io
import build_addr_country
import clarion_format
import fa_encode
import frames
import icode_io
import settle


def icode(codec='latin-1'):
    """Two countries; the second is 246 with labels COUNTRY, COUNTY, KUNTA, SETTLEMENT."""
    d = bytearray(0x3000)
    struct.pack_into('>HH', d, 0x1000, 0, 2)
    for o, (iso, labels) in enumerate(((20, ['COUNTRY', 'TOWN']), (246, ['COUNTRY', 'COUNTY', 'KUNTA', 'SETTLEMENT']))):
        at = 0x1000 + 4 + o * icode_io.ENTRY
        struct.pack_into('>HH', d, at, icode_io.ENTRY // 2, iso)
        d[at + 4:at + 4 + 7 * icode_io.SLOT] = icode_io.iso_slots(labels)
    base = 0x2000
    struct.pack_into('>IH', d, 0x800 + 6 * 13, 0x300, 1)                      # volume table at 0x1800
    struct.pack_into('>HIIIHHBB', d, 0x1800, 0xffff, 0x400, 0, 0, 0, 0, 5, 1)  # one area frame at 0x2000
    frame = bytearray()

    def put(rows):
        at = len(frame)
        frame.extend(rows)
        frame.extend(b'\0' * (-len(frame) % 4))
        return at

    def enc(s):
        return s.encode(codec)
    top_len = len(icode_io.pack_list([icode_io.pack_row(b'AA', 0, 0, 0, leaf=True), icode_io.pack_row(b'FI', 0, 0, 1)], 0xffffffff))
    put(b'\0' * top_len)
    leaves = icode_io.pack_list([icode_io.pack_row(enc(n), 0, 0, i, leaf=True)
                                 for i, n in enumerate(('HAAPANIEMI', 'JYVÄSKYLÄ', 'PALOKKA'))], 0, deepest=True)
    s_at = put(leaves)
    kunta = icode_io.pack_list([icode_io.pack_row(enc('JÄMSÄ'), 0, 0, 0, leaf=True),
                                icode_io.pack_row(enc('JYVÄSKYLÄ'), s_at // 2, len(leaves) // 2, 1)], 0)
    k_at = put(kunta)
    county = icode_io.pack_list([icode_io.pack_row(b'ITA', 0, 0, 0, leaf=True),
                                 icode_io.pack_row(b'KESKI', k_at // 2, len(kunta) // 2, 1)], 0)
    c_at = put(county)
    frame[0:top_len] = icode_io.pack_list([icode_io.pack_row(b'AA', 0, 0, 0, leaf=True),
                                           icode_io.pack_row(b'FI', c_at // 2, len(county) // 2, 1)], 0xffffffff)
    d[base:base + len(frame)] = frame
    return bytes(d)


class Ladder(unittest.TestCase):
    def test_municipality(self):
        ordinal, path, settlements, labels = build_addr_country.ladder(icode(), 246, 'JYVÄSKYLÄ', 'latin-1')
        self.assertEqual((ordinal, path), (1, (1, 1, 1)))
        self.assertEqual(settlements, {'HAAPANIEMI': 0, 'JYVÄSKYLÄ': 1, 'PALOKKA': 2})
        self.assertEqual(len(labels), 4)

    def test_unknown_municipality(self):
        with self.assertRaises(SystemExit):
            build_addr_country.ladder(icode(), 246, 'HELSINKI', 'latin-1')

    def test_frames_and_ordinal(self):
        d = icode()
        self.assertEqual(icode_io.area_frames(d), [(5, 0x2000)])
        self.assertEqual(icode_io.iso_ordinal(d, 246), 1)
        with self.assertRaises(KeyError):
            icode_io.iso_ordinal(d, 352)


class Entries(unittest.TestCase):
    def rows(self):
        def r(street, num, town, lat):
            return dict(street=street, num=num, letter='', post=40100, lat=lat, lon=25.7, town=town)
        return [r('Kauppakatu', '1', 'Jyväskylä', 62.24), r('Kauppakatu', '4', '', 62.241),
                r('Kauppakatu', '2', 'Palokka', 62.3), r('Šokkikuja', '1', 'Jyväskylä', 62.2),
                r('Улица', '1', 'Jyväskylä', 62.2)]

    def test_groups_by_settlement(self):
        ents, odd = build_addr_country.entries(self.rows(), {'JYVÄSKYLÄ': 11, 'PALOKKA': 30}, 'JYVÄSKYLÄ', lambda s: s)
        self.assertEqual([(e['name'], e['town'], e['nums']) for e in ents],
                         [('KAUPPAKATU', 'JYVÄSKYLÄ', [1, 4]), ('KAUPPAKATU', 'PALOKKA', [2]), ('ŠOKKIKUJA', 'JYVÄSKYLÄ', [1])])
        self.assertEqual(ents[2]['key'], 'SOKKIKUJA')
        self.assertEqual([o['street'] for o in odd], ['УЛИЦА'])


class Place(unittest.TestCase):
    def links(self):
        """KATU as two links running east, 100 m each, in one parcel."""
        lat, lon = 62.2401, 25.7401
        pts = [dict(word=55 + 5 * i, link=1 + i // 2 if i else 1, role=r, lon=lon + 0.002 * i, lat=lat)
               for i, r in enumerate(('start', 'end', 'end'))]
        pts[1]['link'], pts[2]['link'] = 1, 2
        rec = dict(code=list(addre_io.parcel_words(lat, lon + 0.001)), unit=22,
                   shapes=[dict(label='1=KATU', at=48, links=[dict(id=1, way=7), dict(id=2, way=7)], points=pts)])
        return addr_links.Links([rec])

    def rows(self, street, nums):
        return [dict(street=street, num=str(n), letter='', post=40100, town='', lat=62.2402,
                     lon=25.7401 + 0.0001 * n) for n in nums]

    def test_ranges_in_number_order(self):
        found = self.rows('Katu', (31, 33, 35, 3, 5, 7)) + self.rows('Polku', (1,))
        ents, _ = build_addr_country.entries(found, {'JYVÄSKYLÄ': 11}, 'JYVÄSKYLÄ', lambda s: s)
        placed, skipped = build_addr_country.place(self.links(), ents)
        self.assertEqual([e['name'] for e in placed], ['KATU'])
        self.assertEqual([(r['lo'], r['hi'], r['link']) for r in placed[0]['ranges']], [(3, 7, 1), (31, 35, 2)])
        self.assertEqual([(s['street'], s['addresses']) for s in skipped], [('POLKU', 1)])


class Sides(unittest.TestCase):
    """One range per link with both sides of the road, as a factory record holds them."""

    def links(self):
        """KATU as one link running east for 220 m, digitised west to east."""
        lat, lon = 62.2401, 25.7401
        pts = [dict(word=55 + 5 * i, link=1, role=r, lon=lon + 0.002 * i, lat=lat)
               for i, r in enumerate(('start', 'vertex', 'end'))]
        rec = dict(code=list(addre_io.parcel_words(lat, lon + 0.001)), unit=22,
                   shapes=[dict(label='1=KATU', at=48, links=[dict(id=1, way=7)], points=pts)])
        return addr_links.Links([rec])

    def ranges(self, houses):
        """houses: (number, metres east along the road, 'N' or 'S' of it)."""
        rows = [dict(street='Katu', num=str(n), letter='', post=40100, town='', lat=62.2401 + (0.0001 if ns == 'N' else -0.0001),
                     lon=25.7401 + east / 51800) for n, east, ns in houses]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(self.links(), ents)
        return placed[0]['ranges']

    def items(self, r):
        return [(it['flag'], it['first'], it['last']) for it in r['items']]

    def test_both_sides_in_one_range(self):
        # numbers rise eastwards: north is left of the record, south right, and right goes first
        (r,) = self.ranges([(1, 20, 'N'), (3, 60, 'N'), (5, 100, 'N'), (2, 20, 'S'), (4, 60, 'S'), (8, 150, 'S')])
        self.assertEqual((r['lo'], r['hi'], r['refs']), (1, 8, ((2, 0x40, 33), (3, 0x40, 43))))
        self.assertEqual(self.items(r), [(0x54, 2, 8), (0x58, 1, 5)])

    def test_numbers_falling_along_the_shape_turn_the_record(self):
        # numbers rise westwards: the record runs end to start, so north is now its right
        (r,) = self.ranges([(5, 20, 'N'), (3, 60, 'N'), (1, 100, 'N'), (6, 20, 'S'), (2, 100, 'S')])
        self.assertEqual(r['refs'], ((4, 0x40, 43), (5, 0x40, 33)))
        self.assertEqual(self.items(r), [(0x54, 1, 5), (0x58, 2, 6)])

    def test_houses_on_one_side_only(self):
        (r,) = self.ranges([(2, 20, 'S'), (4, 60, 'S')])
        self.assertEqual(self.items(r), [(0x54, 2, 4)])
        (r,) = self.ranges([(7, 20, 'N')])
        self.assertEqual(self.items(r), [(0x58, 7, 7)])

    def test_a_side_that_runs_against_the_link(self):
        # up the north side and back down the south, as a loop road is numbered
        (r,) = self.ranges([(1, 20, 'N'), (2, 60, 'N'), (3, 100, 'N'), (4, 140, 'N'), (5, 140, 'S'), (6, 100, 'S')])
        self.assertEqual(self.items(r), [(0x64, 6, 5), (0x68, 1, 4)])

    def test_a_number_on_two_buildings_is_placed_once(self):
        # the register lists each building: 4 again, farther from the road and on the other side
        rows = [dict(street='Katu', num=n, letter=le, post=40100, town='', lat=62.2401 + dlat, lon=25.7401 + east / 51800)
                for n, le, east, dlat in (('2', '', 20, -0.0001), ('4', 'A', 60, -0.0001), ('4', 'B', 150, 0.0004))]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        (e,), _ = build_addr_country.place(self.links(), ents)
        self.assertEqual([self.items(r) for r in e['ranges']], [[(0x54, 2, 4)]])

    def test_file_has_one_record_and_row_per_link(self):
        houses = [(1, 20, 'N'), (3, 60, 'N'), (2, 20, 'S'), (4, 60, 'S')]
        for fmt in (clarion_format.X08_02, None):
            e = dict(name='KATU', ladder=(6, 6, 2, 4, 11), ranges=self.ranges(houses) + self.ranges([(9, 200, 'N')]))
            out, keys, _ = addr_links.addre_mixed([e], lambda e: e['ladder'], addr_links.word_keys,
                                                  frames.addre_prefix((4800, 1280, -1200, 4080), '14/04/12/01', fmt),
                                                  frames.addre_df(fmt), 6, 3, fmt=fmt)
            a = addre_io.Addre(out)
            recs = [addre_io.parse_name_record(n, a.fmt) for n in a.names]
            self.assertEqual([(r['flags'], r['name'], addre_io.parse_items(r['items'])) for r in recs],
                             [(0x18, 'KATU', [(0x54, 2, 4), (0x58, 1, 3)]), (0x19, None, [(0x58, 9, 9)])])
            rows = [addre_io.parse_range_record(a.srec(i)) for i in range(a.nsrec)]
            self.assertEqual([(r['rel'], r['flags'], r['lo'], r['hi'], r['name_idx']) for r in rows],
                             [(0, 0, 1, 4, 0), (0, 0x8000, 9, 9, 1)])

    def test_a_street_with_no_numbers_is_a_plain_row(self):
        rows = [dict(street='Katu', num='B', letter='', post=40100, town='', lat=62.2402, lon=25.7405)]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        (e,), _ = build_addr_country.place(self.links(), ents)
        self.assertNotIn('ranges', e)
        e['ladder'] = (6, 6, 2, 4, 11)
        fmt = clarion_format.X08_02
        out, _, _ = addr_links.addre_mixed([e], lambda e: e['ladder'], addr_links.word_keys,
                                           frames.addre_prefix((4800, 1280, -1200, 4080), '14/04/12/01', fmt),
                                           frames.addre_df(fmt), 6, 3, fmt=fmt)
        a = addre_io.Addre(out)
        self.assertEqual(addre_io.parse_name_record(a.names[0], fmt)['items'], b'')
        self.assertEqual(a.srec(0)[:8], bytes.fromhex('7f7f0000ffffffff'))


class Join(unittest.TestCase):
    """Ranges run on from link to link: the numbers no house has go by distance."""

    def links(self, cut=False):
        """KATU as three links running east, 104 m each; `cut` leaves the middle one out."""
        lat, lon = 62.2401, 25.7401
        pts = [dict(word=55 + 5 * i, link=max(i, 1), role='end' if i else 'start', lon=round(lon + 0.002 * i, 7), lat=lat)
               for i in range(4)]
        shapes = [dict(label='1=KATU', at=48, links=[dict(id=k, way=7) for k in (1, 2, 3)], points=pts)]
        if cut:
            west = [dict(p) for p in pts[:2]]
            east = [dict(pts[2], role='start', link=3, word=155), dict(pts[3], word=160)]
            shapes = [dict(label='1=KATU', at=48, links=[dict(id=1, way=7)], points=west),
                      dict(label='1=KATU', at=148, links=[dict(id=3, way=8)], points=east)]
        return addr_links.Links([dict(code=list(addre_io.parcel_words(lat, lon + 0.001)), unit=22, shapes=shapes)])

    def ranges(self, houses, **how):
        """houses: (number, metres east along the road, 'N' or 'S' of it)."""
        rows = [dict(street='Katu', num=str(n), letter='', post=40100, town='', lat=62.2401 + (0.0001 if ns == 'N' else -0.0001),
                     lon=25.7401 + east / 51845) for n, east, ns in houses]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(self.links(**how), ents, join_up=True)
        return [(r['link'], [(it['flag'], it['first'], it['last']) for it in r['items']], r['addresses'])
                for r in placed[0]['ranges']]

    def test_numbers_between_two_houses_go_by_distance(self):
        # 5 at 50 m, 31 at 250 m: 7 to 29 are 15.4 m apart, the links end at 104 and 207 m
        self.assertEqual(self.ranges([(3, 20, 'S'), (5, 50, 'S'), (31, 250, 'S'), (33, 290, 'S')]),
                         [(1, [(0x54, 1, 11)], 2), (2, [(0x54, 13, 25)], 0), (3, [(0x54, 27, 33)], 2)])

    def test_each_parity_on_its_own_side(self):
        got = self.ranges([(1, 20, 'S'), (2, 20, 'N'), (21, 250, 'S'), (22, 250, 'N')])
        self.assertEqual(got, [(1, [(0x54, 1, 7), (0x58, 2, 8)], 2), (2, [(0x54, 9, 17), (0x58, 10, 18)], 0),
                               (3, [(0x54, 19, 21), (0x58, 20, 22)], 2)])

    def test_numbers_below_the_lowest_house_run_from_the_streets_end(self):
        # 21 at 250 m: 1 to 19 are 22.7 m apart from the west end
        self.assertEqual(self.ranges([(21, 250, 'S'), (23, 290, 'S')]),
                         [(1, [(0x54, 1, 7)], 0), (2, [(0x54, 9, 17)], 0), (3, [(0x54, 19, 23)], 2)])

    def test_numbers_falling_eastwards(self):
        # the street starts at its east end: the records run east to west, south is their left
        # 7 to 13 are 54 m apart going west from 290 m
        self.assertEqual(self.ranges([(5, 290, 'S'), (15, 20, 'S')]),
                         [(3, [(0x58, 1, 7)], 1), (2, [(0x58, 9, 11)], 0), (1, [(0x58, 13, 15)], 1)])

    def test_one_house_takes_the_numbers_below_it(self):
        self.assertEqual(self.ranges([(8, 150, 'N')]), [(2, [(0x58, 2, 8)], 1)])

    def test_links_that_do_not_meet(self):
        # no link between 104 and 207 m: a number there goes to the nearer side
        self.assertEqual(self.ranges([(1, 50, 'S'), (27, 250, 'S')], cut=True),
                         [(1, [(0x54, 1, 13)], 1), (3, [(0x54, 15, 27)], 1)])

    def test_an_entry_with_no_numbers_takes_the_range_of_its_link(self):
        rows = [dict(street='Katu', num=n, letter='', post=40100, town=t, lat=62.24, lon=25.7401 + 50 / 51845)
                for n, t in (('1', 'X'), ('B', 'Y'))]
        ents, _ = build_addr_country.entries(rows, {'X': 0, 'Y': 1}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(self.links(), ents, join_up=True)
        self.assertEqual([e['town'] for e in placed], ['X', 'Y'])
        self.assertIs(placed[1]['ranges'][0], placed[0]['ranges'][0])

    def test_a_road_that_runs_on_from_another_municipality(self):
        # town X has the west link, town Y the east: Y's numbers do not start again at 1
        rows = [dict(street='Katu', num=str(n), letter='', post=40100, town=t, lat=62.24, lon=25.7401 + east / 51845)
                for n, east, t in ((1, 20, 'X'), (5, 80, 'X'), (41, 250, 'Y'), (45, 290, 'Y'))]
        ents, _ = build_addr_country.entries(rows, {'X': 0, 'Y': 1}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(self.links(), ents, join_up=True)
        self.assertEqual([[(r['link'], r['lo'], r['hi']) for r in e['ranges']] for e in placed], [[(1, 1, 5)], [(3, 41, 45)]])

    def test_a_link_with_houses_of_two_entries_reads_the_same_in_both(self):
        rows = [dict(street='Katu', num=str(n), letter='', post=40100, town=t, lat=62.24, lon=25.7401 + east / 51845)
                for n, east, t in ((1, 20, 'X'), (3, 50, 'Y'), (5, 80, 'X'))]
        ents, _ = build_addr_country.entries(rows, {'X': 0, 'Y': 1}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(self.links(), ents, join_up=True)
        self.assertEqual([[(r['link'], r['lo'], r['hi'], r['items']) for r in e['ranges']] for e in placed],
                         [[(1, 1, 5, placed[0]['ranges'][0]['items'])]] * 2)

    def test_houses_next_to_each_other_are_left_alone(self):
        self.assertEqual(self.ranges([(1, 50, 'S'), (3, 150, 'S'), (5, 250, 'S')]),
                         [(1, [(0x54, 1, 1)], 1), (2, [(0x54, 3, 3)], 1), (3, [(0x54, 5, 5)], 1)])


class Settlements(unittest.TestCase):
    def test_a_range_goes_under_the_settlements_of_its_addresses(self):
        village, town = ('KYLÄ', (6, 1, 1, 0)), ('KAUPUNKI', (6, 1, 1, 1))
        a, b, c = (dict(num=n) for n in '123')
        at = {id(a): (village, town), id(b): (village, town), id(c): (town,)}
        low, high = dict(lo=1, hi=2, of=[a, b], addresses=2), dict(lo=3, hi=3, of=[c], addresses=1)
        street = dict(name='KATU', key='KATU', ladder=(6, 1, 1), posts=[], addresses=[a, b, c], ranges=[low, high])
        ents = build_addr_country.under_settlements([street], lambda r: at[id(r)])
        self.assertEqual([(e['town'], e['ladder'], e['n']) for e in ents], [('KYLÄ', (6, 1, 1, 0), 2), ('KAUPUNKI', (6, 1, 1, 1), 3)])
        self.assertEqual([e['ranges'] for e in ents], [[low], [low, high]])
        self.assertIs(ents[0]['ranges'][0], ents[1]['ranges'][0])      # the same numbers under each

    def test_a_link_with_addresses_of_two_settlements_goes_under_both(self):
        here, there = ('A', (6, 1, 1, 0)), ('B', (6, 1, 1, 1))
        rows = [dict(num=str(n)) for n in (1, 2, 3)]
        at = {id(rows[0]): (here,), id(rows[1]): (here,), id(rows[2]): (there,)}
        street = dict(name='KATU', key='KATU', ladder=(6, 1, 1), posts=[], addresses=rows,
                      ranges=[dict(lo=1, hi=3, of=rows, addresses=3)])
        ents = build_addr_country.under_settlements([street], lambda r: at[id(r)])
        self.assertEqual([(e['town'], e['n'], e['ranges'][0]['hi']) for e in ents], [('A', 2, 3), ('B', 1, 3)])

    def test_a_street_with_no_numbers(self):
        here = ('A', (6, 1, 1, 0))
        rows = [dict(num='B')]
        street = dict(name='KATU', key='KATU', ladder=(6, 1, 1), posts=[], addresses=rows, link=dict(la=1, lo=2), n=1)
        (e,) = build_addr_country.under_settlements([street], lambda r: (here,))
        self.assertEqual((e['town'], e['link'], e['nums'], 'ranges' in e), ('A', dict(la=1, lo=2), [], False))


class ParcelLine(unittest.TestCase):
    """A link the tile cuts at a parcel line is one record, with a hop group at the line."""
    BOX_W, BOX_E = [25.71875, 2987 / 48, 0.03125, 1 / 48], [25.75, 2987 / 48, 0.03125, 1 / 48]
    LAT = 62.2401

    def piece(self, box, lons, link, way=7):
        pts = [dict(word=55 + 5 * i, link=link, role=r, lon=lon, lat=self.LAT)
               for i, (r, lon) in enumerate(zip(('start', 'end'), lons))]
        return dict(code=list(addre_io.parcel_words(self.LAT, box[0] + 0.01)), unit=22, box=box,
                    shapes=[dict(label='1=KATU', at=48, links=[dict(id=link, way=way)], points=pts)])

    def links(self, east=(25.75, 25.76), way=7):
        """KATU running east over the line at 25.75: 520 m west of it, 520 m east."""
        on_line = 25.71875 + 2047 / 2048 * 0.03125
        return addr_links.Links([self.piece(self.BOX_W, (25.74, on_line), 1), self.piece(self.BOX_E, east, 3, way)])

    def place(self, links, nums):
        """nums: (number, degrees east of 25.74), all south of the road."""
        rows = [dict(street='Katu', num=str(n), letter='', post=40100, town='', lat=self.LAT - 0.0001, lon=25.74 + d)
                for n, d in nums]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        return build_addr_country.place(links, ents)[0]

    def test_one_record_over_the_line(self):
        west, east = (addre_io.parcel_words(self.LAT, lon) for lon in (25.74, 25.76))
        (r,) = self.place(self.links(), [(1, 0.002), (3, 0.008), (5, 0.012), (7, 0.018)])[0]['ranges']
        self.assertEqual((r['lo'], r['hi'], (r['la'], r['lo_word'])), (1, 7, west))
        self.assertEqual(r['refs'], ((2, 0x40, 33), (2, 0x40, 38), (0x50, 0x40, 33), (3, 0x40, 38)))
        self.assertEqual([(it['flag'], it['first'], it['last']) for it in r['items']], [(0x54, 1, 7)])
        # numbers falling eastwards: the record starts in the east parcel and hops west
        (r,) = self.place(self.links(), [(7, 0.002), (5, 0.008), (3, 0.012), (1, 0.018)])[0]['ranges']
        self.assertEqual((r['la'], r['lo_word']), east)
        self.assertEqual(r['refs'], ((4, 0x40, 38), (4, 0x40, 33), (0x70, 0x40, 38), (5, 0x40, 33)))
        self.assertEqual([(it['flag'], it['first'], it['last']) for it in r['items']], [(0x58, 1, 7)])

    def test_a_piece_drawn_the_other_way(self):
        # the east piece runs towards the line: its groups fall, and say so in their tags
        links = self.links(east=(25.76, 25.75))
        (r,) = self.place(links, [(1, 0.002), (3, 0.008), (5, 0.012), (7, 0.018)])[0]['ranges']
        self.assertEqual(r['refs'], ((2, 0x40, 33), (2, 0x40, 38), (0x50, 0x40, 38), (5, 0x40, 33)))
        self.assertEqual([(it['flag'], it['first'], it['last']) for it in r['items']], [(0x54, 1, 7)])
        (r,) = self.place(links, [(7, 0.002), (5, 0.008), (3, 0.012), (1, 0.018)])[0]['ranges']
        self.assertEqual(r['refs'], ((2, 0x40, 33), (2, 0x40, 38), (0x70, 0x40, 38), (5, 0x40, 33)))
        self.assertEqual([(it['flag'], it['first'], it['last']) for it in r['items']], [(0x58, 1, 7)])

    def test_a_link_that_stops_on_the_line_is_left_out(self):
        links = self.links(way=8)
        self.assertEqual(self.place(links, [(1, 0.002), (3, 0.008)]), [])
        self.assertEqual([(c['addresses'], c['why']) for c in links.cut], [(2, 'stops on a parcel line')])

    def test_a_plain_row_runs_node_to_node(self):
        links = self.links()
        e = dict(street='KATU', name='KATU', town='X', lat=self.LAT, lon=25.742, anchor=(25.742, self.LAT), low=(25.741, self.LAT))
        link = addr_links.link_refs(links, e, *addr_links.pick(links, e))
        self.assertEqual(link['refs'], ((2, 0x40, 33), (2, 0x40, 38), (0x50, 0x40, 33), (3, 0x40, 38)))


class Resolve(unittest.TestCase):
    """addr_resolve's checks of where a record's groups land in the tile."""
    NODES = [(100, 500), (900, 500), (2047, 500), (0, 500), (300, 500), (0, 600)]

    class Card:
        def __init__(self, recs, styled):
            self.recs, self.codec, self.styled = recs, {'T': 'latin-1'}, {'T': styled}

        def records(self, row, col):
            return 'T', self.recs

    def record(self, col, chains, fmt=clarion_format.X08_03):
        """(parcel code, record, {node: v}) of a level-1 record at column `col` of one row."""
        old, fa_encode.FMT = fa_encode.FMT, fmt
        try:
            plain, _ = fa_encode.build_record(1, 93, 949, 11, col, self.NODES, chains, country=246)
        finally:
            fa_encode.FMT = old
        ix = fa_encode.build_record.last_index
        v, node = {}, [c[0][1] for c in chains]
        for S in ix['shapes']:
            ends = [chains[S['chain']][0][1]] + [b for _, _, b, _ in chains[S['chain']]]
            for (w, _, _), n in zip(S['points'], ends):
                v[n] = w - ix['unit']
        return ix['code'], plain, v

    def bad(self, recs, code, refs, styled=False):
        rec = dict(lat=code[0], lon=code[1], refs=refs)
        return [b for b in addr_resolve.resolve(self.Card(recs, styled), rec, 'KATU')[1] if 'shape label' not in b]

    def test_a_link_header_without_its_style_word(self):
        chain = [[(1, 0, 1, []), (2, 1, 2, [])]]
        code, plain, v = self.record(5, chain)
        refs = [(2, 0x40, v[1]), (3, 0x40, v[2])]
        self.assertEqual(v[2] - v[1], 4, 'a QY8 tile leaves an unchanged style word out')
        self.assertEqual(self.bad({code: (plain, None)}, code, [(2, 0x40, v[0]), (3, 0x40, v[1])], styled=True), [])
        self.assertIn('the link header has no style word', ' '.join(self.bad({code: (plain, None)}, code, refs, styled=True)))
        self.assertNotIn('style word', ' '.join(self.bad({code: (plain, None)}, code, refs)))
        code, plain, v = self.record(5, chain, clarion_format.X08_02)
        self.assertEqual(v[2] - v[1], 5, 'a QY7 tile repeats it')
        self.assertNotIn('style word', ' '.join(self.bad({code: (plain, None)}, code, [(2, 0x40, v[1]), (3, 0x40, v[2])], styled=True)))

    def test_a_record_that_ends_on_a_parcel_line(self):
        west, pw, vw = self.record(5, [[(1, 1, 2, [])]])
        east, pe, ve = self.record(6, [[(1, 3, 4, [])]])
        recs = {west: (pw, None), east: (pe, None)}
        self.assertEqual(self.bad(recs, west, [(2, 0x40, vw[1]), (3, 0x40, vw[2])]),
                         ['group (3, 64, %d): the record ends on a parcel line' % vw[2]])
        self.assertEqual(self.bad(recs, west, [(2, 0x40, vw[1]), (2, 0x40, vw[2]), (0x50, 0x40, ve[3]), (3, 0x40, ve[4])]), [])
        off, po, vo = self.record(6, [[(1, 5, 4, [])]])
        self.assertIn('the hop does not go on from the same place',
                      ' '.join(self.bad({west: (pw, None), off: (po, None)}, west,
                                        [(2, 0x40, vw[1]), (2, 0x40, vw[2]), (0x50, 0x40, vo[5]), (3, 0x40, vo[4])])))


class Layout(unittest.TestCase):
    """addr_resolve's checks of a record's items and rows."""

    def rec(self, *items):
        return dict(items=b''.join(addre_io.house_item(first, last, side) for side, first, last in items))

    def test_factory_shapes_pass(self):
        row = dict(rel=0, lo=1, hi=8)
        self.assertEqual(addr_resolve.layout(self.rec((0x54, 2, 8), (0x58, 1, 5)), [row]), [])
        self.assertEqual(addr_resolve.layout(self.rec((0x68, 8, 1)), [row]), [])
        self.assertEqual(addr_resolve.layout(self.rec(), [dict(rel=0x7f7f, lo=0xffff, hi=0xffff)]), [])

    def test_wrong_order_and_wrong_row(self):
        self.assertEqual(addr_resolve.layout(self.rec((0x58, 1, 5), (0x54, 2, 8)), []), ['items for sides 0x58 0x54'])
        self.assertEqual(addr_resolve.layout(self.rec((0x54, 2, 8)), [dict(rel=0, lo=2, hi=6)]), ['row 2-6 over items 2-8'])


class Streets(unittest.TestCase):
    """addr_resolve's checks across the records of a file."""

    def rec(self, name, *items, lat=1, refs=((2, 0x40, 0), (3, 0x40, 9))):
        return dict(name=name, lat=lat, lon=2, refs=refs,
                    items=b''.join(addre_io.house_item(first, last, side) for side, first, last in items))

    def test_a_link_reads_the_same_under_each_settlement(self):
        s = addr_resolve.Streets()
        self.assertEqual(s.add(self.rec('KATU', (0x54, 1, 5)), 'KATU'), [])
        self.assertEqual(s.add(self.rec('KATU', (0x54, 1, 5)), 'KATU'), [])
        self.assertEqual(s.add(self.rec('KATU', (0x54, 1, 3)), 'KATU'),
                         ['other numbers than the same link holds under another settlement'])
        self.assertEqual(s.add(self.rec('TIE', (0x54, 1, 3)), 'TIE'), [])

    def test_joined(self):
        s = addr_resolve.Streets()
        s.add(self.rec('KATU', (0x54, 1, 5), (0x58, 2, 8)), 'KATU')
        s.add(self.rec(None, (0x54, 13, 7), (0x58, 10, 10), lat=3), 'KATU')
        s.add(self.rec('TIE', (0x54, 1, 5), lat=4), 'TIE')
        self.assertTrue(s.ok())
        self.assertEqual((s.joined, s.pairs), (2, 2))
        s.add(self.rec('KUJA', (0x54, 1, 5), lat=5), 'KUJA')
        s.add(self.rec(None, (0x54, 9, 11), lat=6), 'KUJA')
        self.assertFalse(s.ok())
        self.assertEqual((s.joined, s.pairs), (2, 3))


class Colon(unittest.TestCase):
    """KAARLE IX:N KATU keeps its colon in the street file; its road label has an apostrophe."""

    def links(self, label):
        lat, lon = 60.4501, 22.2701
        pts = [dict(word=55 + 5 * i, link=1, role=r, lon=lon + 0.002 * i, lat=lat) for i, r in enumerate(('start', 'end'))]
        return addr_links.Links([dict(code=list(addre_io.parcel_words(lat, lon + 0.001)), unit=22,
                                      shapes=[dict(label=label, at=48, links=[dict(id=1, way=7)], points=pts)])])

    def place(self, label):
        rows = [dict(street='Kaarle IX:n katu', num='1', letter='', post=100, town='', lat=60.4502, lon=22.2705)]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        return build_addr_country.place(self.links(label), ents)

    def test_found_on_the_apostrophe_label(self):
        placed, skipped = self.place("1=KAARLE IX'N KATU")
        self.assertEqual(([e['name'] for e in placed], skipped), (['KAARLE IX:N KATU'], []))
        self.assertEqual(placed[0]['ranges'][0]['label'], "1=KAARLE IX'N KATU")

    def test_a_label_that_kept_the_colon_is_no_road_of_the_name(self):
        placed, skipped = self.place('1=KAARLE IX:N KATU')
        self.assertEqual((placed, [s['street'] for s in skipped]), ([], ['KAARLE IX:N KATU']))

    def test_formats(self):
        # the 2014 QY8 Finnish file spells the street KAARLE IX'N KATU, as its label
        self.assertTrue(clarion_format.X08_02.colon_names)
        self.assertFalse(clarion_format.X08_03.colon_names)

    def test_keys(self):
        # ADDRE007 of the QY7 card: KAARLE I.., IX N K.., KATU; no key starts at N
        self.assertEqual(addr_links.word_keys('KAARLE IX:N KATU'),
                         [(b'KAARLE IX N KATU', False), (b'IX N KATU', True), (b'KATU', True)])

    def test_resolve_reads_the_label(self):
        R = Resolve()
        code, plain, v = R.record(5, [[(1, 0, 1, [])]])
        bg = None
        card = Resolve.Card({code: (plain, bg)}, False)
        rec = dict(lat=code[0], lon=code[1], refs=[(2, 0x40, v[0]), (3, 0x40, v[1])])
        old = addr_resolve.shape_index.shapes_of

        def labelled(text):
            def shapes_of(plain, bg, codec):
                c, u, shapes = old(plain, bg, codec)
                for s in shapes:
                    s['label'] = text
                return c, u, shapes
            return shapes_of
        try:
            addr_resolve.shape_index.shapes_of = labelled("1=KAARLE IX'N KATU")
            self.assertEqual(addr_resolve.resolve(card, rec, 'KAARLE IX:N KATU')[1], [])
            addr_resolve.shape_index.shapes_of = labelled('1=KAARLE IX:N KATU')
            self.assertIn('shape label', addr_resolve.resolve(card, rec, 'KAARLE IX:N KATU')[1][0])
        finally:
            addr_resolve.shape_index.shapes_of = old


class Near(unittest.TestCase):
    """The by-name grid and numpy matching a whole country uses give what the plain walk gives."""

    def links(self, far=False):
        lat, lon = 62.2401, 25.7401
        recs = []
        for k, (la0, lo0) in enumerate([(lat, lon)] + ([(lat + 1.0, lon)] if far else [])):
            pts = [dict(word=55 + 5 * i, link=1, role=r, lon=lo0 + 0.002 * i, lat=la0)
                   for i, r in enumerate(('start', 'vertex', 'end'))]
            recs.append(dict(code=list(addre_io.parcel_words(la0, lo0 + 0.001)), unit=22,
                             shapes=[dict(label='1=KATU', at=48 + k, links=[dict(id=1, way=7)], points=pts)]))
        return recs

    def rows(self, nums, lat=62.2402):
        return [dict(street='Katu', num=str(n), letter='', post=40100, town='', lat=lat + 0.00005 * (n % 2) - 0.0001,
                     lon=25.7401 + 0.0003 * n) for n in nums]

    def test_same_ranges_as_the_plain_walk(self):
        ents = lambda: build_addr_country.entries(self.rows((1, 2, 3, 4, 5, 8, 9)), {'X': 0}, 'X', lambda s: s)[0]
        plain = build_addr_country.place(addr_links.Links(self.links()), ents())[0]
        near = addr_links.Links([], named_only=True)
        for R in self.links(far=True):
            near.add(R, named_only=True)
        local = build_addr_country.place(near, ents(), addr_links.NAME_REACH)[0]
        strip = lambda es: [[{k: v for k, v in r.items() if k != 'points'} for r in e['ranges']] for e in es]
        self.assertEqual(strip(local), strip(plain))
        self.assertEqual([p['along'] for p in local[0]['ranges'][0]['points']],
                         [p['along'] for p in plain[0]['ranges'][0]['points']])

    def test_a_road_of_the_name_out_of_reach_is_not_used(self):
        near = addr_links.Links([], named_only=True)
        for R in self.links(far=True)[1:]:          # only the road 110 km north
            near.add(R, named_only=True)
        ents, _ = build_addr_country.entries(self.rows((1, 2)), {'X': 0}, 'X', lambda s: s)
        placed, skipped = build_addr_country.place(near, ents, addr_links.NAME_REACH)
        self.assertEqual((placed, [s['street'] for s in skipped]), ([], ['KATU']))
        self.assertEqual(near.grid, {})

    def test_far_address_is_unmatched(self):
        near = addr_links.Links([], named_only=True)
        for R in self.links():
            near.add(R, named_only=True)
        ents, _ = build_addr_country.entries(self.rows((1,)) + self.rows((3,), lat=62.30), {'X': 0}, 'X', lambda s: s)
        placed, _ = build_addr_country.place(near, ents, addr_links.NAME_REACH)
        self.assertEqual([(r['lo'], r['hi']) for r in placed[0]['ranges']], [(1, 1)])
        self.assertEqual([a['num'] for a in placed[0]['unmatched']], ['3'])


class Keys(unittest.TestCase):
    def test_a_name_with_no_letters_has_no_key(self):
        self.assertEqual(addr_links.word_keys(''), [])
        self.assertEqual(addr_links.word_keys(' '), [])


class Bilingual(unittest.TestCase):
    def test_a_street_under_both_names(self):
        fold = settle.folder(clarion_format.X08_02)
        rows = [dict(street='Mannerheimintie', street_swe='Mannerheimvägen', num='1', post=100, lat=60.17, lon=24.94),
                dict(street='Kauppakatu', street_swe='', num='2', post=100, lat=60.17, lon=24.95),
                dict(street='Iso-Roobertinkatu', street_swe='Iso Roobertinkatu', num='3', post=100, lat=60.16, lon=24.94)]
        ents, _ = build_addr_country.group(rows, lambda r: ('HELSINKI', (6, 4, 4, 3, 1)), lambda s: s,
                                           lambda r: build_addr_country.names_of(r, fold))
        self.assertEqual([(e['name'], e['n']) for e in ents],
                         [('ISO-ROOBERTINKATU', 1), ('KAUPPAKATU', 1), ('MANNERHEIMINTIE', 1), ('MANNERHEIMVÄGEN', 1)])
        self.assertEqual({e['ladder'] for e in ents}, {(6, 4, 4, 3, 1)})

    def test_name_on_the_other_language_label(self):
        fold = settle.folder(clarion_format.X08_02)
        lad = (6, 4, 4, 3, 1)
        r1 = dict(street='Kaupungintalonkatu', street_swe='Rådhusgatan')
        r2 = dict(street='Uusikatu', street_swe='')
        skipped = [dict(street='KAUPUNGINTALONKATU', ladder=lad, rows=[r1]), dict(street='UUSIKATU', ladder=lad, rows=[r2])]
        missing, other = build_addr_country.other_language(skipped, [dict(name='RÅDHUSGATAN', ladder=lad)],
                                                           lambda s: s, fold)
        self.assertEqual(([m['street'] for m in missing], [o['street'] for o in other]), (['UUSIKATU'], ['KAUPUNGINTALONKATU']))


if __name__ == '__main__':
    unittest.main()
