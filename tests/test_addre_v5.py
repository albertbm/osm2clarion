#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The V05 ADDRE layout of an X08-02 (QY7) card: records, the compressed street block and the header."""
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

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


if __name__ == '__main__':
    unittest.main()
