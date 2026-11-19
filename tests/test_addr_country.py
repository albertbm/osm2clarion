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
import addre_io
import build_addr_country
import clarion_format
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


class Colon(unittest.TestCase):
    def test_a_name_with_a_colon_is_left_out(self):
        rows = [dict(street='Kaarle IX:n katu', num='1', letter='', post=100, town='', lat=60.45, lon=22.27)]
        ents, _ = build_addr_country.entries(rows, {'X': 0}, 'X', lambda s: s)
        for e in ents:
            e['ladder'] = (6, 1, 1, 0)
        placed, skipped = build_addr_country.place(addr_links.Links([]), ents, addr_links.NAME_REACH)
        self.assertEqual((placed, [s['why'][:8] for s in skipped]), ([], ['a colon ']))


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
