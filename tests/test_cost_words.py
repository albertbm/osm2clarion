# SPDX-License-Identifier: GPL-3.0-or-later
"""Link length and travel-time words double per multiplier code, as the factory cards do."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import rd_encode


class CostWords(unittest.TestCase):
    def test_short_link_is_code_zero(self):
        a, b = rd_encode.cost_words(1000, 90)
        self.assertEqual(b, 1000)
        self.assertEqual(a, 0x8000 | 400)                       # 40.0 s

    def test_long_link_doubles_not_quadruples(self):
        # factory L2 links of 4.1-4.9 km carry code 1 with values 2101-2473 (B30R070R)
        a, b = rd_encode.cost_words(4859, 90)
        self.assertEqual(b, 1 << 12 | 2430)
        self.assertEqual(rd_encode.unscaled(b), 4860)
        self.assertEqual(a, 0x8000 | 1944)
        a, b = rd_encode.cost_words(8261, 90)
        self.assertEqual(b, 2 << 12 | 2065)                      # factory: code 2, value 2065
        self.assertEqual(rd_encode.unscaled(b), 8260)

    def test_time_code_one_starts_at_2048(self):
        # a slow 1 km path: 1000 m at 5 km/h is 7200 tenths, code 1 value 3600
        a, b = rd_encode.cost_words(1000, 5)
        self.assertEqual(a, 0x8000 | 1 << 12 | 3600)
        self.assertEqual(rd_encode.unscaled(a, rd_encode.TIME_UNITS), 7200)

    def test_too_long_raises(self):
        with self.assertRaises(ValueError):
            rd_encode.scaled(4094 * 32 + 64)


if __name__ == '__main__':
    unittest.main()
