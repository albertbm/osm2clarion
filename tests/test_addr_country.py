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
import icode_io


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


if __name__ == '__main__':
    unittest.main()
