#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The two map formats in clarion_format: X08-03-00 (QY8) and X08-02-00 (QY7).

Built in code. One case reads a factory QY7 tile and skips without it:

    OSM2CLARION_QY7_TILE=B68R048R.DAT python3 -m unittest tests.test_format
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

import clarion_format
import clarion_profile
import clarion_tile
import rd_build
import rd_encode
import road_strip
import tile_header
import tile_recode
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

    def test_recode_synthetic(self):
        d, _ = sec2_level2_tile(QY7)
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, 'a.DAT'), os.path.join(tmp, 'b.DAT')
            Path(src).write_bytes(d)
            fmt, nrec, nz, differ = tile_recode.recode(src, out)
            self.assertEqual((fmt, nrec, nz, differ), (QY7, 2, 2, []))
            self.assertEqual(Path(out).read_bytes(), d)


class TestRoadStrip(unittest.TestCase):

    def test_empty_section2_dirs(self):
        self.assertEqual(rd_encode.make_dir(1, 0x68, 952, {})['kind'], 0x1046)
        self.assertEqual(rd_encode.make_dir(2, 0x68, 952, {})['x'], 952)
        d = rd_encode.make_dir(2, 0x68, 952, {}, QY7)
        self.assertEqual((d['kind'], d['body'], d['typ'], d['x']), (0x2046, 512, 0x91, 0xffb8))

    def test_extended_marker(self):
        self.assertTrue(tile_scan.is_extended(0x800000d0, 0x110))
        self.assertTrue(tile_scan.is_extended(0xffffffff, 0x7fffffff))
        self.assertFalse(tile_scan.is_extended(0x7fffffff, 0x7fffffff))
        self.assertFalse(tile_scan.is_extended(0xd0, 0x110))

    def test_cut_road_block(self):
        bg, names, road, extra = b'B' * 8, b'N' * 4, b'R' * 12, b'\x02\x42\0\0'
        w = [0] * 22
        w[2], w[20] = 0xfa, 0xf6
        w[7], w[8] = 12, 3
        w[9], w[10] = 15, 2
        w[11], w[12] = 17, 1
        for i in (13, 15, 17):
            w[i] = 0xffff
        rec = struct.pack('>22H', *w) + extra + road + bg + names
        out = road_strip.cut_road_block(rec)
        v = struct.unpack_from('>22H', out)
        self.assertEqual((v[7], v[8], v[9], v[10], v[11], v[12], v[20]), (0xffff, 0, 11, 2, 13, 1, 0xf6))
        self.assertEqual(out[44:], bg + names)
        self.assertIs(road_strip.cut_road_block(out), out)

    def test_strip_synthetic(self):
        d, _ = sec2_level2_tile(QY7)
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, 'B68R048R.DAT'), os.path.join(tmp, 'out.DAT')
            Path(src).write_bytes(d)
            cell, n_in, nkept, _ = road_strip.strip(src, out, QY7)
            self.assertEqual((cell, n_in, nkept), ('B68R048R', 2, 0))
            b = Path(out).read_bytes()
            self.assertEqual(tile_header.check(b[:0x1000]), [])
            with tile_scan.TileScan(b) as t:
                self.assertEqual([s for s, _ in t.sections()], [1, 2])
                self.assertEqual(list(t.records()), [])
                self.assertEqual(len(list(t._dirs(2))), 2)
            with self.assertRaises(SystemExit):
                road_strip.strip(src, out, QY8)


class TestQY7Roads(unittest.TestCase):

    def test_latin1_labels(self):
        self.assertEqual(QY7.text('JYVÄSKYLÄ'), b'JYV\xc4SKYL\xc4')
        self.assertEqual(QY7.text('ČEŠ'), b'CES')
        self.assertEqual(QY8.text('Ä'), 'Ä'.encode('utf-8'))

    def test_link_end_classes(self):
        # one node of class 8 with ends on a class-8 and a class-9 link
        q8 = rd_encode.unit_head([8], [2])
        q7 = rd_encode.unit_head([8], [2], [[8, 9]])
        s8, s9 = rd_encode.pair_slot(8), rd_encode.pair_slot(9)
        self.assertEqual(q8[4 + 2 * s8:6 + 2 * s8], [1, 2])
        self.assertEqual(q7[4 + 2 * s8:6 + 2 * s8], [1, 1])
        self.assertEqual(q7[5 + 2 * s9], 1)

    def test_level2_parts_are_separate_records(self):
        a, b = b'A' * 60, b'B' * 30
        d = rd_encode.make_dir(2, 0x68, 952, {3: {5: (a, b)}}, QY7)
        s = d['slots'][3]
        self.assertEqual([r['k'] for r in s['recs']], [10, 11])
        self.assertEqual([QY7.inflate(r['raw']) for r in s['recs']], [a, b])
        self.assertEqual(struct.unpack_from('>II', s['body'], 8 * 11)[1], len(b) // 2)

    def test_replace_names_keeps_other_sections(self):
        w = [1, 2, 0xfa, 0x347a, 0x2aef, 0, 0] + [0xffff, 0] * 6 + [0, 0, 0]
        w[9], w[10] = 11, 2
        w[11], w[12] = 13, 1
        w[15], w[16] = 14, 1
        w[19], w[20], w[21] = 0, 0xf6, 0
        rec = struct.pack('>22H', *w) + b'G' * 8 + b'N' * 4 + b'P' * 4
        out = rd_build.replace_names(rec, b'M' * 8)
        v = struct.unpack_from('>22H', out)
        self.assertEqual((v[9], v[10], v[11], v[12], v[15], v[16]), (11, 2, 13, 2, 15, 1))
        self.assertEqual(out[44:], b'G' * 8 + b'M' * 8 + b'P' * 4)


@unittest.skipUnless(os.environ.get('OSM2CLARION_QY7_TILE'), 'set OSM2CLARION_QY7_TILE to a factory QY7 tile')
class TestFactoryQY7(unittest.TestCase):

    def test_recode_is_byte_identical(self):
        src = os.environ['OSM2CLARION_QY7_TILE']
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'out.DAT')
            fmt, _, _, differ = tile_recode.recode(src, out)
            self.assertIs(fmt, QY7)
            self.assertEqual(differ, [])
            self.assertEqual(tile_recode.sha(out), tile_recode.sha(src))


if __name__ == '__main__':
    unittest.main()
