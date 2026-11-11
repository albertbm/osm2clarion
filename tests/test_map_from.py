#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The index edit behind --map-from and the leftover purge: dropping a file row keeps the rest
and re-sums the folder in its parent."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import drstr_add


def index(root_rows, dir_rows):
    recs = [('/', 0, 0, root_rows), ('DIR', 1, 0, dir_rows)]
    off = 0x28 * len(recs)
    head, body = b'', b''
    for name, own, parent, rows in recs:
        head += struct.pack('<20sHHH', name.encode().ljust(20, b'\0'), own, parent, len(rows)) + b'\0' * 10
        head += struct.pack('<I', off + len(body))
        body += b''.join(drstr_add.child_bytes(n, t, s) for n, t, s in rows)
    return head + body


class TestRemove(unittest.TestCase):
    def test_remove_keeps_the_rest_and_resums(self):
        d = index([('DIR', 1, 600)], [('A.DAT', 0, 100), ('B.DAT', 0, 200), ('C.DAT', 0, 300)])
        out = drstr_add.remove(d, 'DIR', ['B.DAT'])
        recs = drstr_add.dir_records(out)
        rows = drstr_add.children(out, recs[1])
        self.assertEqual([(n, s) for n, _, s in rows], [('A.DAT', 100), ('C.DAT', 300)])
        self.assertEqual(drstr_add.children(out, recs[0]), [('DIR', 1, 400)])
        self.assertEqual(len(out), len(d) - 0x20)

    def test_unlisted_name_is_refused(self):
        d = index([('DIR', 1, 100)], [('A.DAT', 0, 100)])
        with self.assertRaises(AssertionError):
            drstr_add.remove(d, 'DIR', ['Z.DAT'])


if __name__ == '__main__':
    unittest.main()
