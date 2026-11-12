#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""card_verify's card-level checks on hand-made file sets: the version stamp, the inventory."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

import card_verify
from card_pack import stamp_text


def stamped(version, body=b'x' * 40):
    return body + stamp_text(version) + body


class TestVersion(unittest.TestCase):
    def test_all_the_units_version_passes(self):
        plain = {'MAPAL001/B1.DAT': stamped('17/09/01/01'), 'REFER001/INTER001.DAT.cmp': b'none'}
        self.assertTrue(card_verify.check_version(plain, '17/09/01/01')['passed'])

    def test_another_version_fails(self):
        plain = {'MAPAL001/B1.DAT': stamped('17/09/01/01'), 'REFER001/ADDRE001.DAT.cmp': stamped('14/11/17/01')}
        r = card_verify.check_version(plain, '17/09/01/01')
        self.assertFalse(r['passed'])
        self.assertIn('REFER001/ADDRE001.DAT.cmp carries 14/11/17/01', r['notes'])

    def test_unstamped_tile_fails(self):
        plain = {'MAPAL001/B1.DAT': b'no stamp', 'REFER001/ADDRE001.DAT.cmp': stamped('17/09/01/01')}
        self.assertFalse(card_verify.check_version(plain, '17/09/01/01')['passed'])

    def test_nothing_stamped_fails(self):
        self.assertFalse(card_verify.check_version({}, '17/09/01/01')['passed'])


class TestInventory(unittest.TestCase):
    m = dict(files=[dict(path='MAPAL001/A.DAT'), dict(path='HOUSE001/H.DAT'), dict(path='DRSTR001.DAT')])

    def test_same_files_pass(self):
        files = {'MAPAL001/A.DAT': b'', 'HOUSE001/H.DAT': b'', 'MMAIN001.DAT': b''}
        self.assertTrue(card_verify.check_inventory(files, self.m, [])['passed'])

    def test_a_vanished_file_fails(self):
        self.assertFalse(card_verify.check_inventory({'MAPAL001/A.DAT': b''}, self.m, [])['passed'])

    def test_a_new_file_fails_unless_allowed(self):
        files = {'MAPAL001/A.DAT': b'', 'HOUSE001/H.DAT': b'', 'REFER001/X.DAT': b''}
        self.assertFalse(card_verify.check_inventory(files, self.m, [])['passed'])
        self.assertTrue(card_verify.check_inventory(files, self.m, ['REFER001/*'])['passed'])


if __name__ == '__main__':
    unittest.main()
