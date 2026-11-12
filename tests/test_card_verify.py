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


class TestTowns(unittest.TestCase):
    def verdict(self, **counts):
        import collections
        return card_verify.towns_verdict(collections.Counter(**counts), collections.Counter(), [])

    def test_all_matching_passes(self):
        self.assertTrue(self.verdict(named=100, match=100)[0])

    def test_no_match_fails(self):
        ok, _, notes = self.verdict(named=100, **{'not in register': 100})
        self.assertFalse(ok)
        self.assertIn('no named record matches the register', notes)

    def test_many_records_off_the_register_fail(self):
        self.assertFalse(self.verdict(named=100, match=90, **{'not in register': 10})[0])
        self.assertTrue(self.verdict(named=1000, match=999, **{'not in register': 1})[0])


class FakeFat:
    """Clusters of 4 bytes; the chain jumps, so reads come in runs."""
    spc, bps = 1, 4

    def __init__(self, chain, data):
        self.order, self.data = chain, data

    def chain(self, c):
        return self.order

    def csec(self, c):
        return c - 2

    def rd(self, off, n):
        return self.data[off:off + n]


class TestStreaming(unittest.TestCase):
    def test_chunks_follow_the_chain(self):
        import fatread
        data = bytes(range(40))
        fs = fatread.Fat32.__new__(fatread.Fat32)
        fs.f = FakeFat([2, 3, 4, 7, 8, 9], data)
        want = data[0:12] + data[20:30]
        self.assertEqual(b''.join(fs.chunks(2, 22)), want)
        self.assertEqual(b''.join(fs.chunks(2, 22, most=4)), want)
        self.assertEqual(list(fs.chunks(2, 0)), [])
        with self.assertRaises(ValueError):
            list(fs.chunks(2, 30))

    def test_a_stamp_split_across_pieces_is_found(self):
        b = stamped('17/09/01/01')
        for cut in range(1, len(b)):
            st = card_verify.Stamps()
            st.feed(b[:cut])
            st.feed(b[cut:])
            self.assertEqual(st.found, {'17/09/01/01'})


if __name__ == '__main__':
    unittest.main()
