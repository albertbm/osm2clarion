# SPDX-License-Identifier: GPL-3.0-or-later
"""The link code of a one-way slip road: shape word 3's top nibble, 3 on both formats."""
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

    def test_slip_ways(self):
        def way(i, hw, d, a, b): return dict(id=i, hw=hw, dir=d, nodes=[[a, 64.0], [b, 64.0]])
        m = 1 / (111320 * 0.4384)                       # one metre of longitude at 64 N
        ways = [way(1, 'trunk_link', 1, 0, 70 * m), way(2, 'trunk_link', 1, 70 * m, 130 * m),   # 130 m in two pieces
                way(3, 'primary_link', 1, 1, 1 + 80 * m),                                        # 80 m on its own
                way(4, 'primary_link', 0, 2, 2 + 300 * m),                                       # long but two-way
                way(5, 'motorway_link', 0, 3, 3 + 20 * m),                                       # any motorway link
                way(6, 'primary', 1, 4, 4 + 500 * m)]
        self.assertEqual(rd_build.slip_ways(ways), {1, 2, 5})

    def test_is_slip(self):
        rd_build.SLIP_WAYS = frozenset({7})
        try:
            self.assertTrue(rd_build.is_slip(link('primary_link', 1)[:3] + (7,) + link('primary_link', 1)[4:]))
            self.assertFalse(rd_build.is_slip(link('primary_link', 1)))
        finally:
            rd_build.SLIP_WAYS = frozenset()

    def test_a_shape_ends_where_the_link_code_changes(self):
        # one chain: a one-way slip piece, a two-way piece of the same connector, a ring piece
        a = ((10, 10), (20, 10), 200.0, 1, None, 'trunk_link', None, None, [], 0, 1, None)
        b = ((20, 10), (30, 10), 60.0, 2, None, 'trunk_link', None, None, [], 0, 0, None)
        ring = ((30, 10), (40, 10), 60.0, 3, None, 'trunk_link', None, None, [], 0, 1, 'roundabout')
        rd_build.SLIP_WAYS = frozenset({1})
        try:
            self.assertEqual(rd_build.link_groups([a, b, ring]), [[0], [1], [2]])
            self.assertEqual(rd_build.link_groups([b, b]), [[0, 1]])
            rd_build.SLIP_WAYS = frozenset({1, 2})
            self.assertEqual(rd_build.link_groups([a, b, ring]), [[0, 1], [2]])
        finally:
            rd_build.SLIP_WAYS = frozenset()

    def test_qy8_word3(self):
        w = self.words(clarion_format.X08_03, slip=True)
        self.assertEqual(w[3], 0x3004)
        self.assertEqual(self.words(clarion_format.X08_03)[3], 0x0004)
        self.assertEqual(self.words(clarion_format.X08_03, ring=True, slip=True)[3], 0x7004)

    def test_qy7_word3(self):
        self.assertEqual(self.words(clarion_format.X08_02, slip=True)[3], 0x3000)
        self.assertEqual(self.words(clarion_format.X08_02)[3], 0x0000)
        self.assertEqual(self.words(clarion_format.X08_02, ring=True, slip=True)[3], 0x7000)

    def test_qy7_graph_code_is_the_shape_code(self):
        rd_build.FMT = clarion_format.X08_02; rd_build.SLIP_WAYS = frozenset({1})
        try:
            self.assertEqual(rd_build.link_code(link('primary_link', 1)), 0x3000)
        finally:
            rd_build.FMT = clarion_format.DEFAULT; rd_build.SLIP_WAYS = frozenset()


if __name__ == '__main__':
    unittest.main()
