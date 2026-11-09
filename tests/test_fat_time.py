# SPDX-License-Identifier: GPL-3.0-or-later
"""fat_time() stamps every FAT write with the wall clock unless $SOURCE_DATE_EPOCH
pins it, which is how build_card.sh makes two runs of the same recipe byte-identical."""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import fatwrite


class FatTime(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.pop('SOURCE_DATE_EPOCH', None)

    def tearDown(self):
        if self.old is None:
            os.environ.pop('SOURCE_DATE_EPOCH', None)
        else:
            os.environ['SOURCE_DATE_EPOCH'] = self.old

    def test_source_date_epoch_overrides_the_ts_argument(self):
        os.environ['SOURCE_DATE_EPOCH'] = '1000000000'  # 2001-09-09
        want = fatwrite.fat_time(1000000000.0)
        self.assertEqual(fatwrite.fat_time(1577836800.0), want)   # 2020-01-01, ignored
        self.assertEqual(fatwrite.fat_time(time.time()), want)

    def test_two_calls_without_the_variable_can_still_differ(self):
        # the pre-fix behaviour, kept working for a caller that wants a real timestamp
        os.environ.pop('SOURCE_DATE_EPOCH', None)
        self.assertEqual(fatwrite.fat_time(1577836800.0), fatwrite.fat_time(1577836800.0))
        self.assertNotEqual(fatwrite.fat_time(1577836800.0), fatwrite.fat_time(1577836800.0 + 86400))

if __name__ == '__main__':
    unittest.main()
