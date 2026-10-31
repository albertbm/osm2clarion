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


def shape_words(sid, kind, typ, attr, x0, y0, deltas, tail=()):
    """One shape as words: int8 deltas unless attr & 0x0100."""
    wide = bool(attr & 0x0100)
    body = []
    for dx, dy in deltas:
        if wide:
            body += [dx & 0xffff, dy & 0xffff]
        else:
            body.append((dx & 0xff) << 8 | (dy & 0xff))
    n = len(deltas)
    w = [0, sid, 0xe000 | n, typ, attr, x0, y0] + body + list(tail)
    w[0] = len(w) | (0x2000 if tail else 0)
    return w


def section(groups):
    """groups: [(kind, [shape words, ...]), ...] -> section words."""
    ng = len(groups)
    head = [ng, 2 * ng + 1]
    body = []
    offs = []
    q = 2 * ng + 1
    for kind, shps in groups:
        offs.append(q)
        for w in shps:
            body += w
            q += len(w)
    head.append(groups[0][0] | len(groups[0][1]))
    for (kind, shps), o in zip(groups[1:], offs[1:]):
        head += [o, kind | len(shps)]
    return tuple(head + body)


class Drawing(unittest.TestCase):

    # the measured sea polygon of BPCL parcel 0bed 120d (south of Hateruma): int16
    # deltas, a zero delta at every switch between parcel border and coast
    SEA = [(0, 0), (2048, 0), (0, 4096), (-621, 0), (0, 0), (-8, -4), (-11, 4), (0, 0),
           (-288, 0), (0, 0), (-6, -5), (-9, -2), (-10, 7), (0, 0), (-1095, 0), (0, -4096), (0, 0)]

    def test_wide_polygon_closes_and_draws_the_coast(self):
        sec = section([(0x8000, [shape_words(1, 0x8000, 0x0121, 0xc500, 0, 0, self.SEA)])])
        s, = jp_dbdir.shapes(sec)
        self.assertEqual((s.kind, s.type, s.scales), (0x80, 0x0121, 7))
        self.assertEqual(s.pts[0], s.pts[-1])
        self.assertEqual(s.breaks, [1, 4, 6, 7, 10, 12])
        runs = jp_dbdir.drawn_runs(s)
        self.assertEqual(runs, [[(1427, 4096), (1419, 4092), (1408, 4096)],
                                [(1120, 4096), (1114, 4091), (1105, 4089), (1095, 4096)]])

    def test_narrow_deltas_groups_points_and_tails(self):
        line = shape_words(7, 0x4000, 0x0120, 0xc000, 100, 200, [(127, -128), (-1, 1)])
        point = shape_words(9, 0, 0x054c, 0xc000, 3000, 4000, [])
        tagged = shape_words(10, 0, 0x01f2, 0xc000, 5, 6, [], tail=(0x8000, 0x0167))
        sec = section([(0x4000, [line]), (0x0000, [point, tagged])])
        a, b, c = jp_dbdir.shapes(sec)
        self.assertEqual(a.pts, [(100, 200), (227, 72), (226, 73)])
        self.assertEqual((b.kind, b.pts, b.tail), (0, [(3000, 4000)], ()))
        self.assertEqual(c.tail, (0x8000, 0x0167))

    def test_count_above_255(self):
        sq = [(10, 0), (0, 10), (-10, 0), (0, -10)]
        shps = [shape_words(i, 0x8000, 0x040a, 0xc000, i, i, sq) for i in range(300)]
        got = list(jp_dbdir.shapes(section([(0x8000, shps)])))
        self.assertEqual(len(got), 300)
        self.assertTrue(all(s.kind == 0x80 and s.pts[0] == s.pts[-1] for s in got))

    def test_bad_group_offset(self):
        sec = list(section([(0x4000, [shape_words(1, 0x4000, 0x0120, 0xc000, 0, 0, [(1, 1)])]),
                            (0x4000, [shape_words(2, 0x4000, 0x0120, 0xc000, 0, 0, [(1, 1)])])]))
        sec[3] += 1
        with self.assertRaises(ValueError):
            list(jp_dbdir.shapes(tuple(sec)))

    def test_record_sections(self):
        sec = section([(0x8000, [shape_words(1, 0x8000, 0x0121, 0xc500, 0, 0, self.SEA)])])
        n = 1 + 3 * 19
        t = [n]
        for i in range(19):
            t += [0, n, len(sec)] if i == 1 else [0xffff, 0xffff, 0]
        rec = struct.pack('>%dH' % (n + len(sec)), *(t + list(sec)))
        got = jp_dbdir.record_sections(rec)
        self.assertEqual(list(got), [1])
        self.assertEqual(got[1], sec)


class DrawingRows(unittest.TestCase):

    def test_blob_refs(self):
        # BPCLD+0xE004 after the key: 29 42 | 01 06 00 00, 2a 0a 00 | 02 06 00 00 ...
        b = bytes.fromhex('2942 0106 0000 2a0a 0002 0600 002b 3c01 0003 0600 00'.replace(' ', ''))
        self.assertEqual(jp_dbdir.blob_refs(b), [(0x42, 1, 6), (0x0a, 2, 6), (0x13c, 3, 6)])
        refs, rest = jp_dbdir.blob_refs(b + bytes.fromhex('d8180000'), 3)
        self.assertEqual((len(refs), rest), (3, bytes.fromhex('d8180000')))

    def test_read_blob_follows_continuations(self):
        data = bytes(range(256)) * 40
        first = data[:3000]
        rest = data[3000:]
        # page 3 holds a short row then the 0x4009 head; page 4 slot 1 the tail
        p3 = [struct.pack('<HH', 10, 0x0009) + b'abcdef',
              struct.pack('<HH', 8 + len(first), 0x4009) + bytes([1]) + (4).to_bytes(3, 'little') + first]
        p4 = [struct.pack('<HH', 4 + len(rest), 0x0009) + rest]
        img = bytes(0x10000) + data_page(p3, size=0x8000) + data_page(p4, size=0x8000)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'BPCLB')
            with open(p, 'wb') as f:
                f.write(img)
            with open(p, 'rb') as f:
                self.assertEqual(jp_dbdir.read_blob(f, (len(data), 2, 3)), data)
                self.assertEqual(jp_dbdir.read_blob(f, (6, 1, 3)), b'abcdef')
                with self.assertRaises(ValueError):
                    jp_dbdir.read_blob(f, (7, 1, 3))

    def test_zlib_blob(self):
        raw = b'\x00\x3a' + bytes(200)
        z = zlib.compress(raw)
        img = data_page([struct.pack('<HH', 4 + len(z), 0x0009) + z], size=0x8000)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'BTPCL13B')
            with open(p, 'wb') as f:
                f.write(img)
            with open(p, 'rb') as f:
                self.assertEqual(jp_dbdir.read_blob(f, (len(z), 1, 1)), raw)


class ParcelKeys(unittest.TestCase):

    def test_bpcl_level1_corner(self):
        # BPCLD row 1: A 0x0bed, B 0x120d; its descriptor says 24.02083N 123.78125E
        self.assertEqual(jp_dbdir.bpcl_level(0x0bed, 0x120d), 1)
        (lon, lat), size = jp_dbdir.bpcl_parcel(0x0bed, 0x120d)
        self.assertEqual((lon, lat), (0x366564, 0x0a8e58))
        self.assertEqual(size, (900, 600))

    def test_bpcl_level2_corner(self):
        self.assertEqual(jp_dbdir.bpcl_level(0x0bd2, 0x123f), 2)
        (lon, lat), size = jp_dbdir.bpcl_parcel(0x0bd2, 0x123f)
        self.assertEqual((lon / 28800, lat / 28800), (123.625, 24.25))
        self.assertEqual(size, (3600, 2400))

    def test_btpcl_cell_is_the_mesh_key(self):
        (lon, lat), size = jp_dbdir.btpcl_parcel(0x139f, 0x1ab4)
        self.assertEqual((lon / 28800, lat / 28800), jp_dbdir.mesh_point(0x139f, 0x1ab4))
        x, y = jp_dbdir.parcel_point(4096, 4096, (lon, lat), size)
        self.assertAlmostEqual(x, jp_dbdir.mesh_point(0x13a0, 0x1ab5)[0])
        self.assertAlmostEqual(y, jp_dbdir.mesh_point(0x13a0, 0x1ab5)[1])

    def test_tokyo_datum_shift(self):
        # Tokyo Datum -> WGS84 near Tokyo moves about -11.6" in lon and +11.6" in lat
        lon, lat = jp_dbdir.tokyo_to_wgs84(139.75, 35.68)
        self.assertAlmostEqual((lon - 139.75) * 3600, -11.6, delta=0.3)
        self.assertAlmostEqual((lat - 35.68) * 3600, 11.6, delta=0.3)


if __name__ == '__main__':
    unittest.main()
