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


if __name__ == '__main__':
    unittest.main()
