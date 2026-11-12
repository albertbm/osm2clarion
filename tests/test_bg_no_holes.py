#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""bg_encode.no_holes and lev_build.no_holes: real holes are cut out, a floating-point sliver
hole is dropped instead of recursing forever, as house_encode.no_holes already does."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

try:
    from shapely.geometry import Polygon
    import bg_encode
    import lev_build
except ImportError:
    bg_encode = None

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


@unittest.skipUnless(bg_encode, 'needs shapely')
class NoHoles(unittest.TestCase):
    def fns(self):
        return (bg_encode.no_holes, lev_build.no_holes)

    def test_real_hole_is_cut(self):
        p = Polygon(SQUARE, [[(4, 4), (6, 4), (6, 6), (4, 6)]])
        for fn in self.fns():
            out = fn(p)
            self.assertTrue(out)
            self.assertFalse(any(q.interiors for q in out))
            self.assertAlmostEqual(sum(q.area for q in out), p.area, places=9)

    def test_sliver_hole_is_dropped(self):
        tiny = [(5, 5), (5 + 1e-14, 5), (5 + 1e-14, 5 + 1e-14), (5, 5 + 1e-14)]
        p = Polygon(SQUARE, [tiny])
        for fn in self.fns():
            out = fn(p)
            self.assertEqual(len(out), 1)
            self.assertFalse(out[0].interiors)

    def test_cut_stays_inside_the_hole(self):
        p = Polygon(SQUARE, [[(2, 2), (8, 2), (8, 3), (3, 3), (3, 8), (2, 8)]])
        a, _, c, _ = p.interiors[0].bounds
        self.assertTrue(a < bg_encode.cut_x(p) < c)


if __name__ == '__main__':
    unittest.main()
