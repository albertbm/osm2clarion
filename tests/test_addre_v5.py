#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The V05 ADDRE layout of an X08-02 (QY7) card: records, the compressed street block and the header."""
import os
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

sys.path.insert(0, str(ROOT / 'tools' / 'addr'))

import addr_links
import addre_io
import clarion_format
import frames
import refer_header

V5 = clarion_format.X08_02
BOUNDS = (4800, 1280, -1200, 4080)
# ADDRE032 on the 2014 QY7 card, name record 1
SAN_STEFANO = bytes.fromhex('c01606009f63d432024006f3034006f8030900ff001f00000000') + b'LARGO SAN STEFANO\0'


def card(n=3):
    """A V05 file of n streets on a five-level ladder, the second with a range row."""
    ladder = (6, 6, 2, 4, 11)
    names, srecs, keys = [], [], []
    for i in range(n):
        name = 'KATU %05d' % i
        ranged = i == 1
        names.append(addre_io.name_record(name, 0xaeaf, 0xdadf, ladder,
                                          addre_io.house_item(1, 9) if ranged else b'', fmt=V5))
        keys.append((name.encode('ascii'), len(srecs), 1))
        srecs.append(addre_io.range_record(0, 1, 9, i, ladder) if ranged else addre_io.street_record(0, i, ladder))
    pre = srecs[0][12:22]
    rows = [r[:12] + nxt[12:22] for r, nxt in zip(srecs, srecs[1:])] + [srecs[-1][:12] + b'\0' * 10]
    d, w = frames.addre_hints(names)
    out, _ = addre_io.assemble(frames.addre_prefix(BOUNDS, '14/04/12/01', V5), keys, rows, names,
                               frames.addre_df(V5), pre, 12, 1, 6, 3, maxkey_hint=d, namewidth_hint=w, fmt=V5)
    return out, keys, rows, names, pre


class NameRecord(unittest.TestCase):
    def test_factory_record(self):
        f = addre_io.parse_name_record(SAN_STEFANO, V5)
        self.assertEqual((f['name'], f['levels'], f['lat'], f['lon']), ('LARGO SAN STEFANO', (31, 0, 0), 0x9f63, 0xd432))
        self.assertEqual(addre_io.name_record(f['name'], f['lat'], f['lon'], f['levels'], f['items'], f['refs'],
                                              f['flags'], fmt=V5), SAN_STEFANO)

    def test_latin1_and_continuation(self):
        r = addre_io.name_record('JYVÄSKYLÄNTIE', 1, 2, (6, 6, 2, 4, 11), fmt=V5)
        self.assertIn('JYVÄSKYLÄNTIE'.encode('latin-1'), r)
        self.assertEqual(addre_io.parse_name_record(r, V5)['name'], 'JYVÄSKYLÄNTIE')
        cont = addre_io.name_record('', 1, 2, (), addre_io.house_item(3, 5), flags=0x19, fmt=V5)
        self.assertEqual(cont[-4:], b'\0\0\0\xff')
        self.assertIsNone(addre_io.parse_name_record(cont, V5)['name'])

    def test_x08_03_record_unchanged(self):
        r = addre_io.name_record('ÞING', 1, 2, (0, 3, 1))
        self.assertEqual(r[-14:], bytes([3, 3, 0, 0, 0, 3, 0, 1]) + 'ÞING'.encode('utf-8') + b'\0')


class FactoryItems(unittest.TestCase):
    """Name records of ADDRE007 on the 2014 QY7 card, items and all."""

    # AAKUNTIE: both sides of one link, the numbers falling along it
    BOTH = bytes.fromhex('c01f060caeafdaee024001fa03400209065400003a31303a3b3a323a065800003a333a3b3a313a00'
                         '050400ff0006000600020004000a41414b554e544945')
    # YLIOPISTONKATU's first two records: a link with houses on its left only, then one on its right
    LEFT = bytes.fromhex('c01c0606aeafdadf02400e7303400e78065800003a313a3b3a313a00'
                         '050700ff0006000600020004000b594c494f504953544f4e4b415455')
    RIGHT = bytes.fromhex('c8100606aeafdadf02400f7003400f75065400003a323a3b3a343a00000000ff')
    # AALLONHARJA, numbered 1, 2, 3 without an odd and an even side
    MIXED = bytes.fromhex('c816060caf6cdb3c024002a0034002a5066400003a333a3b3a333a00066800003a343a3b3a343a00000000ff')
    ROWS = [('0006000600020004000a000000000001000a00000100', BOTH),
            ('0006000600020004000b00000000000100010013c19b', LEFT),
            ('0006000600020004000b00008000000200040013c19c', RIGHT)]

    def test_items(self):
        got = {n: addre_io.parse_items(addre_io.parse_name_record(getattr(self, n), V5)['items'])
               for n in ('BOTH', 'LEFT', 'RIGHT', 'MIXED')}
        self.assertEqual(got, dict(BOTH=[(0x54, 10, 2), (0x58, 3, 1)], LEFT=[(0x58, 1, 1)], RIGHT=[(0x54, 2, 4)],
                                   MIXED=[(0x64, 3, 3), (0x68, 4, 4)]))

    def test_records_rebuild(self):
        for raw in (self.BOTH, self.LEFT, self.RIGHT, self.MIXED):
            f = addre_io.parse_name_record(raw, V5)
            items = b''.join(addre_io.house_item(first, last, side) for side, first, last in addre_io.parse_items(f['items']))
            self.assertEqual(addre_io.name_record(f['name'] or '', f['lat'], f['lon'], f['levels'], items, f['refs'],
                                                  f['flags'], fmt=V5), raw)

    def test_a_row_spans_both_sides(self):
        # the stored row opens on its ladder; its numbers are the lowest and highest of the items
        for row, raw in self.ROWS:
            ladder, rel, flags, lo, hi = struct.unpack_from('>10s4H', bytes.fromhex(row))
            nums = [n for _, first, last in addre_io.parse_items(addre_io.parse_name_record(raw, V5)['items'])
                    for n in (first, last)]
            self.assertEqual((rel, lo, hi), (0, min(nums), max(nums)))

    def test_side_byte(self):
        self.assertEqual([addre_io.side_byte(left, nums) for left, nums in
                          ((False, [2, 4]), (True, [1]), (False, [3, 4]), (True, [1, 2, 3]))], [0x54, 0x58, 0x64, 0x68])


@unittest.skipUnless(os.environ.get('OSM2CLARION_FACTORY_ADDRE'), 'set OSM2CLARION_FACTORY_ADDRE to an inflated factory ADDRE file')
class FactoryFile(unittest.TestCase):
    """The layout the writer follows, on every record of a factory file of either format."""

    def test_layout(self):
        a = addre_io.Addre(Path(os.environ['OSM2CLARION_FACTORY_ADDRE']).read_bytes())
        rows = {}
        for i in range(a.nsrec):
            r = addre_io.parse_range_record(a.srec(i))
            rows.setdefault(r['name_idx'], []).append(r)
        street, plain = [], 0
        for i, raw in enumerate(a.names):
            f = addre_io.parse_name_record(raw, a.fmt)
            items = addre_io.parse_items(f['items'])
            self.assertEqual(b''.join(addre_io.house_item(first, last, side) for side, first, last in items), f['items'])
            self.assertIn([side & 0x0c for side, _, _ in items], ([], [4], [8], [4, 8]))
            self.assertEqual(f['flags'], 0x19 if f['name'] is None else 0x18)
            if f['name'] is not None:
                self.assertEqual(street, sorted(street))     # a street's records run in number order
                street = []
            nums = [n for _, first, last in items for n in (first, last)]
            for side, first, last in items:
                if side & 0x30 == 0x10:
                    self.assertEqual(first % 2, last % 2)
            for r in rows[i]:
                if items:
                    self.assertEqual((r['rel'], r['lo'], r['hi']), (0, min(nums), max(nums)))
                    self.assertEqual(r['flags'] & 0x8000, 0 if f['name'] is not None else 0x8000)
                else:
                    self.assertEqual((r['rel'], r['lo'], r['hi'], f['name'] is None), (0x7f7f, 0xffff, 0xffff, False))
                    plain += 1
            if items:
                street.append((min(nums), max(nums)))
        self.assertTrue(plain)


class File(unittest.TestCase):
    def test_reads_back(self):
        out, keys, rows, names, pre = card()
        a = addre_io.Addre(out)
        self.assertTrue(a.fmt.refer_v5)
        self.assertEqual((a.keys, a.names, a.srec_pre), (sorted(keys), names, pre))
        self.assertEqual([a.srec(i) for i in range(a.nsrec)], rows)
        self.assertEqual((a.area, a.ntier, a.cr, a.kw), (6, 3, 12, 1))

    def test_layout(self):
        out, keys, rows, names, pre = card()
        self.assertTrue(refer_header.check(out[:0x1000]))
        self.assertEqual(out[0x101a:0x101c], b'\0\0')
        o = addre_io.unaddr(struct.unpack_from('>I', out, 0x1012)[0])
        self.assertEqual(out[o:o + 4], b'\x4e\x20\0\0')
        size = struct.unpack_from('>I', out, o + 4)[0] * 2
        self.assertEqual(size % 4, 0)
        plain = zlib.decompress(out[o + 8:o + 8 + size])
        self.assertEqual(plain, (pre + b''.join(rows))[:len(rows) * 22])
        self.assertEqual(plain[:10], struct.pack('>5H', 6, 6, 2, 4, 11))
        dfr = addre_io.unaddr(struct.unpack_from('>I', out, 0x800 + 42)[0])
        self.assertEqual(out[dfr + 0x100:dfr + 0x102], b'\x78\x9c')
        tail = addre_io.unaddr(struct.unpack_from('>I', out, dfr + 0x92)[0])
        self.assertEqual(out[tail:tail + 4], b'\x01\xf4\0\0')

    def test_two_street_blocks(self):
        out, keys, rows, names, pre = card(addre_io.V5_PER + 5)
        o = addre_io.unaddr(struct.unpack_from('>I', out, 0x1012)[0])
        first = struct.unpack_from('>I', out, o + 4)[0] * 2
        self.assertEqual(len(zlib.decompress(out[o + 12:o + 12 + first])), addre_io.V5_PER * 22)
        a = addre_io.Addre(out)
        self.assertEqual((a.nsrec, len(a.names), len(a.blocks)), (len(rows), len(names), -(-len(names) // 500)))
        self.assertEqual(a.srec(len(rows) - 1), rows[-1])


    def test_block_table_that_ends_with_the_file(self):
        # 21 name blocks: the table is 256 bytes exactly and no zero entry follows it
        out, keys, rows, names, pre = card(21 * addre_io.NAMES_PER_BLOCK)
        tail = addre_io.unaddr(struct.unpack_from('>I', out, addre_io.unaddr(struct.unpack_from('>I', out, 0x800 + 42)[0]) + 0x92)[0])
        self.assertEqual(len(out) - tail, 256)
        a = addre_io.Addre(out)
        self.assertEqual((len(a.blocks), a.names), (21, names))


class Keys(unittest.TestCase):
    def test_shortest(self):
        # ADDRE014 on the QY7 card: AEULE, AEULEGRABEN, AEULESTRASSE, AEULEWEG, AKMEIN
        full = [b'AEULE', b'AEULEGRABEN', b'AEULESTRASSE', b'AEULEWEG', b'AKMEIN']
        self.assertEqual(addr_links.shortest_keys(full), [b'AEULE', b'AEULEG', b'AEULES', b'AEULEW', b'AK'])
        self.assertEqual(addr_links.shortest_keys([b'KATU']), [b'K'])

    def test_word_keys(self):
        self.assertEqual(addr_links.word_keys('ALVAR AALLON KATU'),
                         [(b'ALVAR AALLON KATU', False), (b'AALLON KATU', True), (b'KATU', True)])
        self.assertEqual(addr_links.word_keys('14 TIE'), [(b'14 TIE', False), (b'TIE', True)])

    def test_shortened_file_finds_every_street(self):
        ladder = (6, 6, 2, 4, 11)
        ents = [dict(name=n, nums=[], link=dict(la=1, lo=2, refs=addre_io.street_refs(10, 20)))
                for n in ('KAUPPAKATU', 'KAUPPAKUJA', 'KOULUTIE')]
        out, keys, _ = addr_links.addre_mixed(ents, lambda e: ladder, addr_links.word_keys,
                                              frames.addre_prefix(BOUNDS, '14/04/12/01', V5), frames.addre_df(V5),
                                              6, 3, fmt=V5, shorten=True)
        self.assertEqual([k[0] for k in keys], [b'KAUPPAKA', b'KAUPPAKU', b'KO'])
        a = addre_io.Addre(out)
        self.assertEqual(a.keys, keys)
        got = [addre_io.parse_name_record(a.names[struct.unpack_from('>I', (a.srec_pre + a.rows)[i * 22:], 18)[0]], V5)['name']
               for i in range(a.nsrec)]
        self.assertEqual(got, ['KAUPPAKATU', 'KAUPPAKUJA', 'KOULUTIE'])


if __name__ == '__main__':
    unittest.main()
