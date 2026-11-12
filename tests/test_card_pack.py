#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""card_pack.mmain: level counts summed from the map headers, db box copied from them."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

import card_pack
import tile_header
from tile_header import BOUNDS, BOUNDS2

EUROPE = (4800, 1280, -1200, 4080)     # the stock ME0 card's box
ICELAND = (4800, 1280, -1680, 3600)    # the ZE1 cut's box, which the Iceland tiles carry
LEVELS = [(1, [(6, 0x80, 1), (1, 0x92, 3)]), (2, [(1, 0x90, 2)])]


def header(box, scale=1, size=0x400):
    """A tile header's first 0x400 bytes; size=0x1000 for a whole MMAIN001.DAT."""
    lv = [(sid, [(l, t, c * scale) for l, t, c in rows]) for sid, rows in LEVELS]
    return bytes(tile_header.author(lv, [], box, '17/09/01/01', 'NAVTEQ 17Q2'))[:size]


class TestMmain(unittest.TestCase):
    def test_box_comes_from_the_map_files(self):
        base = header(EUROPE, size=0x1000)
        out = card_pack.mmain(base, [header(ICELAND), header(ICELAND, 2)])
        self.assertEqual(struct.unpack_from('>4h', out, BOUNDS), ICELAND)
        self.assertEqual(struct.unpack_from('>2h', out, BOUNDS2), (1280, -1680))
        counts = [struct.unpack_from('>BBH', out, p) for p in (0x1b0, 0x1b4, 0x1ba)]
        self.assertEqual(counts, [(6, 0x80, 3), (1, 0x92, 9), (1, 0x90, 6)])
        # everything outside the box words and the level table is the base's
        same = [(0, 0x184), (0x18c, 0x1b0), (0x1be, 0x5d0), (0x5d4, 0x1000)]
        for a, b in same:
            self.assertEqual(out[a:b], base[a:b], hex(a))

    def test_matching_base_is_unchanged_apart_from_counts(self):
        base = header(ICELAND, size=0x1000)
        out = card_pack.mmain(base, [header(ICELAND)])
        self.assertEqual(out, base)

    def test_disagreeing_headers_refuse(self):
        with self.assertRaises(ValueError):
            card_pack.mmain(header(EUROPE), [header(ICELAND), header(EUROPE)])

    def test_no_headers_keeps_the_base_box(self):
        base = header(EUROPE, size=0x1000)
        out = card_pack.mmain(base, [])
        self.assertEqual(struct.unpack_from('>4h', out, BOUNDS), EUROPE)


class TestRestamp(unittest.TestCase):
    def test_stamps_change_and_nothing_else(self):
        old, new = card_pack.stamp_text('17/09/01/01'), card_pack.stamp_text('14/11/17/01')
        tile = b'\1' * 0xfd + old + b'\2' * 300
        packed = (b'head' + old) * 30000          # spans two 64 KiB blocks
        c, i = card_pack.wrap(packed)
        files = {'MAPAL001/T.DAT': tile, 'REFER001/A.DAT.cmp': c, 'REFER001/A.DAT.ind': i,
                 'REFER001/B.DAT': b'no stamp', 'REFER001/ICODE001.DAT': old}
        exp, bare = card_pack.restamped(files, '17/09/01/01', '14/11/17/01')
        self.assertEqual(exp['MAPAL001/T.DAT'][0], tile.replace(old, new))
        cmp_, ind = exp['REFER001/A.DAT.cmp'][0], exp['REFER001/A.DAT.ind'][0]
        self.assertEqual(card_pack.unwrap(cmp_, ind), packed.replace(old, new))
        self.assertEqual(exp['REFER001/A.DAT.cmp'][2], len(packed))
        self.assertEqual(bare, ['REFER001/B.DAT'])
        self.assertEqual(exp['REFER001/B.DAT'][0], b'no stamp')
        self.assertNotIn('REFER001/ICODE001.DAT', exp)

    def test_same_version_keeps_the_packed_bytes(self):
        old = card_pack.stamp_text('17/09/01/01')
        c, i = card_pack.wrap(old)
        exp, _ = card_pack.restamped({'R/A.DAT.cmp': c, 'R/A.DAT.ind': i}, '17/09/01/01', '17/09/01/01')
        self.assertEqual((exp['R/A.DAT.cmp'][0], exp['R/A.DAT.ind'][0]), (c, i))


if __name__ == '__main__':
    unittest.main()
