#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""house_encode.no_holes: a real hole gets cut out, a floating-point sliver hole does not
send the recursion into the card 082 build's infinite loop (measured hole area 1e-29 deg^2
on a 1e-08 deg^2 polygon)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

try:
    from shapely.geometry import Polygon
    import house_encode
except ImportError:
    house_encode = None


@unittest.skipUnless(house_encode, 'needs shapely')
class NoHoles(unittest.TestCase):
    def test_real_hole_is_cut(self):
        p = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(4, 4), (6, 4), (6, 6), (4, 6)]])
        out = house_encode.no_holes(p)
        self.assertTrue(out)
        for piece in out:
            self.assertFalse(piece.interiors)
        self.assertAlmostEqual(sum(q.area for q in out), p.area, places=9)

    def test_degenerate_hole_is_dropped(self):
        # a hole far too small to represent at this scale must not force a cut at all
        tiny = [(5, 5), (5 + 1e-14, 5), (5 + 1e-14, 5 + 1e-14), (5, 5 + 1e-14)]
        p = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [tiny])
        out = house_encode.no_holes(p)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].interiors)
        self.assertAlmostEqual(out[0].area, 100.0, places=6)


if __name__ == '__main__':
    unittest.main()
