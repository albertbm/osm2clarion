# SPDX-License-Identifier: GPL-3.0-or-later
"""The link code of a one-way slip road: shape word 3's top nibble, 3 on QY8 and absent on QY7."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import clarion_format, fa_encode, rd_build


def link(hw, d, junction=None):
    return (0, 1, 10.0, 1, 'A', hw, None, None, [], 0, d, junction)


class LinkCode(unittest.TestCase):
    def tearDown(self):
        fa_encode.FMT = clarion_format.DEFAULT

    def words(self, fmt, **kw):
        fa_encode.FMT = fmt
        return fa_encode.shape_words([(1, 0, 1, [])], [(10, 10), (20, 20)], **kw)

    def test_is_slip(self):
        self.assertTrue(rd_build.is_slip(link('primary_link', 1)))
        self.assertTrue(rd_build.is_slip(link('trunk_link', -1)))
        self.assertFalse(rd_build.is_slip(link('primary_link', 0)))
        self.assertFalse(rd_build.is_slip(link('primary', 1)))

    def test_qy8_word3(self):
        w = self.words(clarion_format.X08_03, slip=True)
        self.assertEqual(w[3], 0x3004)
        self.assertEqual(self.words(clarion_format.X08_03)[3], 0x0004)
        self.assertEqual(self.words(clarion_format.X08_03, ring=True, slip=True)[3], 0x7004)

    def test_qy7_unchanged(self):
        self.assertEqual(self.words(clarion_format.X08_02, slip=True), self.words(clarion_format.X08_02))


if __name__ == '__main__':
    unittest.main()
