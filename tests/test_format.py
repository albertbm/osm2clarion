#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The two map formats in clarion_format: X08-03-00 (QY8) and X08-02-00 (QY7). Built in code."""
import os
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import clarion_format
import clarion_profile
import clarion_tile
import tile_header
import tile_scan

QY7, QY8 = clarion_format.X08_02, clarion_format.X08_03
PLAIN = bytes(range(256)) * 7 + b'\x00\x19' * 50


def pairs_body(lengths, size):
    """A slot body: (compressed words, plain words) per record, the rest 0xff."""
    b = bytearray(b'\xff' * size)
    for k, (ln, x) in enumerate(lengths):
        struct.pack_into('>II', b, 8 * k, ln, x)
    return bytes(b)


def sec2_level2_tile(fmt):
    """A tile with one section-2 level-2 directory holding two compressed records."""
    recs = [fmt.deflate(PLAIN), fmt.deflate(PLAIN[:300])]
    body = clarion_format.slot_body(2, dict(fmt.levels[2])[2])
    slot = dict(tag=0, body=pairs_body([(len(r) // 2, n // 2) for r, n in zip(recs, (len(PLAIN), 300))], body),
                recs=[dict(k=k, raw=r, words=0) for k, r in enumerate(recs)])
    D = dict(kind=0x2046, ab=(0x20, 0x20), y=0x68, x=0xffb8, typ=dict(fmt.levels[2])[2],
             box=(1, 2, 3, 4), body=body, slots={0: slot}, nslots=32)
    sec1 = dict(id=1, levels=[dict(level=l, typ=t, dirs=[]) for l, t in fmt.levels[1]])
    sec2 = dict(id=2, levels=[dict(level=l, typ=t, dirs=[D] if l == 2 else []) for l, t in fmt.levels[2]])
    t = dict(sections=[sec1, sec2])
    for sec in t['sections']:
        nw, _, _ = clarion_tile.table_words(sec)
        sec['count'] = -(-2 * nw // 256)
    t['header'] = tile_header.author(tile_header.level_groups(t, fmt), [(1, []), (2, [(2, 0, (1, 2, 3, 4))])],
                                     clarion_profile.EUROPE_BOUNDS, '14/04/12/01', fmt=fmt)
    return clarion_tile.write_tile(t), recs


class TestFormats(unittest.TestCase):

    def test_qy8_values_unchanged(self):
        self.assertIs(clarion_format.DEFAULT, QY8)
        self.assertEqual(tile_header.IDENT[0xa0], 'FORMAT VERSION X08-03-00')
        self.assertEqual(tile_header.SECT_SLOTS, 300)
        self.assertEqual(tile_header.LEVELS[2], [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)])

    def test_qy7_header(self):
        h, _ = sec2_level2_tile(QY7)
        self.assertIs(clarion_format.of_header(h), QY7)
        self.assertEqual(h[0x40:0x4e].decode('utf-16-be'), 'HITACHI')
        self.assertEqual(h[0x3ac:0x3ae], b'\0\0')
        self.assertEqual(h[0x4ac:0x5cc], bytes(0x120))
        self.assertEqual(h[0x2a0:0x2ac], bytes(12), 'zero fill after the level table')
        self.assertEqual(struct.unpack_from('>IH', h, 0x800 + 6 * 340)[0], 0xffffffff)
        self.assertEqual(h[0xffe:0x1000], b'\0\0')
        self.assertEqual(tile_header.check(h[:0x1000]), [])

    def test_qy8_header_reads_as_qy8(self):
        h, _ = sec2_level2_tile(QY8)
        self.assertIs(clarion_format.of_header(h), QY8)
        self.assertEqual(tile_header.check(h[:0x1000]), [])

    def test_unknown_header_is_qy8(self):
        self.assertIs(clarion_format.of_header(b'\xff' * 0x1000), QY8)

    def test_deflate(self):
        for fmt, magic, pad in ((QY7, b'\x78\x9c', 4), (QY8, b'\x58\x85', 2)):
            for n in range(1, 40):
                z = fmt.deflate(PLAIN[:n * 37])
                self.assertEqual(z[:2], magic)
                self.assertEqual(len(z) % pad, 0)
                self.assertEqual(fmt.inflate(z), PLAIN[:n * 37])

    def test_qy8_deflate_is_the_encoders(self):
        for n in range(1, 40):
            c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
            z = c.compress(PLAIN[:n * 37]) + c.flush()
            self.assertEqual(QY8.deflate(PLAIN[:n * 37]), z + b'\0' * (len(z) % 2))

    def test_slot_body(self):
        self.assertEqual(clarion_format.slot_body(1, 0x80), 512)
        self.assertEqual(clarion_format.slot_body(2, 0x91), 512)
        self.assertEqual(clarion_format.slot_body(2, 0x81), 256)
        self.assertEqual(clarion_format.slot_body(4, 0x93), 256)

    def test_qy7_section2_level2_reads_back(self):
        d, recs = sec2_level2_tile(QY7)
        with tile_scan.TileScan(d) as t:
            got = list(t.records(section=2))
        self.assertEqual([r.raw() for r in got], recs)
        self.assertEqual(got[0].plain(), PLAIN)
        t = clarion_tile.read_tile(d)
        self.assertEqual(clarion_tile.write_tile(t), d)

    def test_profile_format(self):
        self.assertIs(clarion_profile.default().fmt, QY8)
        self.assertIs(clarion_profile.Profile({'format': 'x08-02'}).fmt, QY7)
        with self.assertRaises(SystemExit):
            clarion_profile.Profile({'format': 'x08-04'})


if __name__ == '__main__':
    unittest.main()
