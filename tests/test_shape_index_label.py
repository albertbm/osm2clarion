#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""rd_build's shape index holds a label as the tile spells it, so the read-back check agrees."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import clarion_format
import rd_build


class ShapeIndexLabel(unittest.TestCase):
    def test_label_as_the_tile_spells_it(self):
        old = rd_build.FMT
        rd_build.FMT = clarion_format.X08_02
        try:
            ix = dict(code=(1, 2), unit=22, shapes=[dict(chain=0, points=[(55, 1, 'start'), (60, 1, 'end')],
                                                       layer=5, cls=8, at=48)])
            link = ((0, 0), (2047, 2047), None, 7, 'Ferry', None, None, None, [])
            R = rd_build.shape_index_record(1, (92, 949, 0, 0), ix, [[0]], {0: 1}, [link],
                                            ['1=TRAVEMÜNDE (D) – HELSINKI (FIN)'], False)
        finally:
            rd_build.FMT = old
        self.assertEqual(R['shapes'][0]['label'], '1=TRAVEMÜNDE (D) ? HELSINKI (FIN)')

    def test_a_colon_is_an_apostrophe(self):
        old = rd_build.UPPER_NAMES
        rd_build.UPPER_NAMES = True
        try:
            self.assertEqual(rd_build.label('Kaarle IX:n katu', '12'), "1=KAARLE IX'N KATU;2=12")
        finally:
            rd_build.UPPER_NAMES = old
        self.assertEqual(rd_build.label(None, '12'), '2=12')


if __name__ == '__main__':
    unittest.main()
