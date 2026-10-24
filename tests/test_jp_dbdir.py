#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks for tools/jp_dbdir.py, the Japanese DBDIR reader.

Every input is built in code from the layouts docs/JP-DBDIR.md records, so this needs
no card. A JP card, where one is mounted read-only, is exercised by `jp_dbdir.py` itself.

    python3 -m unittest tests.test_jp_dbdir -v
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import jp_dbdir


def header(name, kind=b'U', page=0x1000, file_id=0x034c):
    b = bytearray(0x1000)
    struct.pack_into('<H', b, 0, file_id)
    b[2:4] = b'\x01\x55'
    b[4:8] = b'flhb'
    struct.pack_into('<H', b, 8, len(name))
    b[10:10 + len(name)] = name
    b[0x30] = kind[0]
    struct.pack_into('<H', b, 0xde, page)
    struct.pack_into('<I', b, 0xe0, 1)
    b[0xe4:0xf7] = b'2015/03/11-22:04:31'
    return bytes(b)


def table_row(cols, present=None):
    """A 0x4001 row: ncols, MSB-first bitmap, then the columns concatenated."""
    present = present or [True] * len(cols)
    nb = (len(cols) + 7) // 8
    bits = 0
    for i, p in enumerate(present):
        if p:
            bits |= 1 << (8 * nb - 1 - i)
    body = bytes([len(cols)]) + bits.to_bytes(nb, 'big') + b''.join(c for c, p in zip(cols, present) if p)
    return struct.pack('<HH', 4 + len(body), 0x4001) + body


def data_page(rows, table_id=0x02f9, size=0x1000):
    b = bytearray(size)
    struct.pack_into('<H', b, 0, table_id)
    b[2:4] = b'\x01\x55'
    o = 4
    for r in rows:
        b[o:o + len(r)] = r
        o += len(r)
    return bytes(b)


class MeshKey(unittest.TestCase):

    def test_tokyo_first_mesh(self):
        # 35.5N 139.0E is the south-west corner of first mesh 5339
        a, b = jp_dbdir.mesh_key(139.0, 35.5)
        self.assertEqual((a, b), (4992, 6816))
        self.assertEqual(jp_dbdir.first_mesh(a, b), 5339)

    def test_roundtrip_corner(self):
        for lon, lat in ((122.98, 24.48), (145.6, 45.41), (139.75, 35.68)):
            a, b = jp_dbdir.mesh_key(lon, lat)
            x, y = jp_dbdir.mesh_point(a, b)
            self.assertLess(lon - x, 1 / 128.0)
            self.assertLess(lat - y, 1 / 192.0)
            self.assertGreaterEqual(lon, x)
            self.assertGreaterEqual(lat, y)

    def test_southern_hemisphere_has_no_key(self):
        # Wellington: the longitude fits, the latitude does not
        with self.assertRaises(ValueError):
            jp_dbdir.mesh_key(174.78, -41.29)


class Rows(unittest.TestCase):

    def test_columns_with_bitmap(self):
        key = struct.pack('<HH', 4992, 6816)
        r = table_row([key, b'\x2a\x2e', b'\x00\x01'], [True, False, True])
        present, body = jp_dbdir.columns(r)
        self.assertEqual(present, [True, False, True])
        self.assertEqual(jp_dbdir.key_of(r), (4992, 6816))
        self.assertEqual(body, key + b'\x00\x01')

    def test_balldd_style_row(self):
        # 21 00 01 00 02 c0: length 0x21, flags 0x0001, two columns, both present
        k, v = b'SYSTEM_FORMAT_VERSION', b'0100'
        body = bytes([2, 0xc0, len(k)]) + k + bytes([len(v)]) + v
        r = struct.pack('<HH', 4 + len(body), 0x0001) + body
        self.assertEqual(r[:6], bytes.fromhex('2100 0100 02c0'))
        present, cols = jp_dbdir.columns(r)
        self.assertEqual(present, [True, True])
        self.assertEqual(cols[1:1 + len(k)], k)

    def test_page_rows_stop_at_free_space(self):
        rows = [table_row([struct.pack('<HH', 4992 + i, 6816)]) for i in range(3)]
        got = list(jp_dbdir.page_rows(data_page(rows)))
        self.assertEqual([r for _, _, r in got], rows)
        self.assertEqual(got[0][0], 4)

    def test_file_walk(self):
        rows = [table_row([struct.pack('<HH', 4992 + i, 6816 + i)]) for i in range(5)]
        img = header(b'BPCLD') + bytes(0xd000) + data_page(rows)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'BPCLD')
            with open(p, 'wb') as f:
                f.write(img)
            h = jp_dbdir.read_header(p)
            self.assertEqual((h.name, h.kind, h.page, h.file_id), ('BPCLD', 'U', 0x1000, 0x034c))
            self.assertEqual(jp_dbdir.table_id(p), 0x02f9)
            keys = [jp_dbdir.key_of(r) for r in jp_dbdir.table_rows(p)]
            self.assertEqual(keys, [(4992 + i, 6816 + i) for i in range(5)])


class Blobs(unittest.TestCase):

    def payload(self):
        """A drawing record: 19-section table, one polygon group with one shape."""
        group = bytes.fromhex('0001 0003 8001 0005 0001'.replace(' ', ''))
        n = 2 + 6 * 19
        tw = n // 2
        t = struct.pack('>H', tw) + struct.pack('>IH', tw, len(group) // 2)
        t += b''.join(struct.pack('>IH', 0xffffffff, 0) for _ in range(18))
        return t + group

    def test_section_table(self):
        t = jp_dbdir.section_table(self.payload())
        self.assertEqual(len(t), 19)
        self.assertEqual(t[0], (58, 5))
        self.assertTrue(all(s is None for s in t[1:]))

    def test_group_headers(self):
        self.assertEqual(dict(jp_dbdir.group_headers(self.payload())), {'polygon': 1})

    def test_blob_file(self):
        raw = self.payload()
        rows = []
        for body in (raw, zlib.compress(raw)):
            rows.append(struct.pack('<HH', 4 + len(body), 0x0009) + body)
        img = header(b'BPCLB', b'B', 0x8000) + bytes(0x8000 - 0x1000) + data_page(rows, size=0x8000)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'BPCLB')
            with open(p, 'wb') as f:
                f.write(img)
            got = list(jp_dbdir.blob_payloads(p))
        self.assertEqual(got, [raw, raw])


if __name__ == '__main__':
    unittest.main()
