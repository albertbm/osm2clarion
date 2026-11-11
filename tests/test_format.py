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
import fa_encode
import rd_build
import rd_encode
import road_strip
import sec4_write
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
            cell, n_in, nkept, ncountry, _ = road_strip.strip(src, out, QY7)
            self.assertEqual((cell, n_in, nkept, ncountry), ('B68R048R', 2, 0, 0))
            b = Path(out).read_bytes()
            self.assertEqual(tile_header.check(b[:0x1000]), [])
            with tile_scan.TileScan(b) as t:
                self.assertEqual([s for s, _ in t.sections()], [1, 2])
                self.assertEqual(list(t.records()), [])
                self.assertEqual(len(list(t._dirs(2))), 2)
            with self.assertRaises(SystemExit):
                road_strip.strip(src, out, QY8)

    def test_strip_keeps_section4_as_country_records(self):
        d, _ = sec2_level2_tile(QY7)
        t = clarion_tile.read_tile(d)
        y, x = 0x68, 0xffb8
        D = sec4_write.make_dir(y, x, {}, 0, fmt=QY7)
        sign = [dict(key=7, groups=[dict(hdr=sec4_write.GROUP_HDR, entries=[
            dict(dir=0x2800, pairs=[(sec4_write.FLAG, 3)], text=b'0=OULU')])])]
        code = sec4_write.parcel_code(y * 32 + 5, 952 * 32 + 9)
        long = sec4_write.encode(dict(code=code, country=246, subs=sign))
        comp = QY7.deflate(long)
        body = bytearray(sec4_write.ABSENT * 32)
        struct.pack_into('>II', body, 8 * 9, len(comp) // 2, len(long) // 2)
        D['slots'][5] = dict(tag=0, body=bytes(body), recs=[dict(k=9, raw=comp, words=len(long) // 2)])
        del D['empty_bodies'][5]
        sec = dict(id=4, levels=[dict(level=1, typ=0x93, dirs=[D])])
        sec['count'] = -(-2 * clarion_tile.table_words(sec)[0] // 256)
        t['sections'].append(sec)
        t['header'] = tile_header.author(tile_header.level_groups(t, QY7),
                                         [(1, []), (2, [(2, 0, (1, 2, 3, 4))]), (4, [(1, 0, (5, 6, 7, 8))])],
                                         clarion_profile.EUROPE_BOUNDS, '14/04/12/01', fmt=QY7)
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, 'B68R048R.DAT'), os.path.join(tmp, 'out.DAT')
            Path(src).write_bytes(clarion_tile.write_tile(t, clarion_tile.default_order(t)))
            self.assertEqual(road_strip.strip(src, out, QY7)[3], 1)
            b = Path(out).read_bytes()
        self.assertEqual(tile_header.check(b[:0x1000]), [])
        f, _ = tile_header.fields(b[:0x1000])
        self.assertEqual(f['box_rows'], [(1, []), (2, []), (4, [(1, 0, (5, 6, 7, 8))])])
        with tile_scan.TileScan(b) as ts:
            recs = list(ts.records(section=4))
            self.assertEqual([(r.slot, r.k) for r in recs], [(5, 9)])
            want = sec4_write.encode(dict(code=code, country=246))
            self.assertEqual(recs[0].plain(), want)
            self.assertEqual(len(want), 56)

    def test_section4_records_follow_the_format(self):
        plain, comp = sec4_write.record(3, 4, 246, fmt=QY7)
        self.assertEqual(comp[:2], b'\x78\x9c')
        self.assertEqual(len(comp) % 4, 0)
        self.assertEqual(zlib.decompress(comp), plain)
        self.assertEqual(sec4_write.record(3, 4, 246)[1][:2], b'\x58\x85')
        # a QY7 directory stores x less 1024; the parcel code takes the mesh's own x
        D = sec4_write.make_dir(0x68, 0xffb8, {2: [1]}, 246, fmt=QY7)
        got = struct.unpack_from('>2H', QY7.inflate(D['slots'][2]['recs'][0]['raw']))
        self.assertEqual(got, sec4_write.parcel_code(0x68 * 32 + 2, 952 * 32 + 1))

    def test_section4_merge_keeps_what_is_there(self):
        D = sec4_write.make_dir(0x68, 952, {2: [1]}, 578)
        first = D['slots'][2]['recs'][0]['raw']
        self.assertEqual(sec4_write.add_records(D, {2: [1, 6], 4: [0]}, 352), 2)
        self.assertEqual([r['k'] for r in D['slots'][2]['recs']], [1, 6])
        self.assertEqual(D['slots'][2]['recs'][0]['raw'], first)
        self.assertEqual(struct.unpack_from('>H', zlib.decompress(D['slots'][4]['recs'][0]['raw']), 16)[0], 352)
        self.assertNotIn(4, D['empty_bodies'])

    def test_section4_header_update(self):
        h = tile_header.author([(1, [(1, 0x92, 3)]), (2, [(1, 0x90, 1)]), (4, [(1, 0x93, 2)])],
                               [(1, []), (2, []), (4, [(1, 0, (10, 5, 20, 30))])],
                               clarion_profile.EUROPE_BOUNDS, '14/04/12/01', fmt=QY7)
        f, _ = tile_header.fields(sec4_write.update_header(bytes(h), 5, (12, 6, 15, 25)))
        self.assertEqual(f['level_groups'][2], (4, [(1, 0x93, 5)]))
        self.assertEqual(f['box_rows'][2], (4, [(1, 0, (12, 5, 15, 30))]))


class TestQY7Roads(unittest.TestCase):

    NODES = [(100, 200), (300, 400), (500, 600), (700, 800)]
    # three links meet at node 1
    CHAINS = [[(1, 0, 1, []), (2, 1, 2, [])], [(3, 1, 3, [])]]

    def shape_record(self, fmt, level=1, **kw):
        old = fa_encode.FMT
        fa_encode.FMT = fmt
        try:
            plain, _ = fa_encode.build_record(level, 0x5d, 949, 11, 23, self.NODES, self.CHAINS, country=246, **kw)
            return struct.unpack('>%dH' % (len(plain) // 2), plain), fa_encode.build_record.last_items
        finally:
            fa_encode.FMT = old

    def test_style_word_two_way_bit(self):
        old = fa_encode.FMT
        try:
            fa_encode.FMT = QY7
            self.assertEqual(fa_encode.style_word(None), 0x201e)
            self.assertEqual(fa_encode.style_word(50), 0x2029)
            self.assertEqual(fa_encode.style_word(50, oneway=True), 0x0029)
            fa_encode.FMT = QY8
            self.assertEqual(fa_encode.style_word(None), fa_encode.STYLE)
            self.assertEqual(fa_encode.style_word(50, oneway=True), 0x0029)
        finally:
            fa_encode.FMT = old

    def test_level1_string_header_and_tail(self):
        w7, _ = self.shape_record(QY7, rings=[False, True])
        w8, _ = self.shape_record(QY8, rings=[False, True])
        g = 22
        self.assertEqual(w7[g], 1, 'one node with three links')
        self.assertEqual(w8[g], 0)
        at = g + 2 * w7[g + 1 + 2 * 5]   # both chains default to layer 5
        self.assertEqual(w7[at + 3:at + 6:2], (0, 0))
        self.assertEqual(w8[at + 3:at + 6:2], (0x0004, 0x8004))
        self.assertEqual(w7[at + w7[at] + 3], 0x7000, 'the ring keeps its code')
        self.assertEqual(w8[at + w8[at] + 3], 0x7004)
        self.assertEqual(w7[17:19], (0xffff, 0), 'no tail')
        self.assertEqual(len(w7), g + 2 * w7[8])
        self.assertEqual(fa_encode.tail_of(struct.pack('>%dH' % len(w7), *w7)), (0, []))
        self.assertEqual(w8[17], 11 + w8[8])

    def test_level2_string_takes_the_ring_code(self):
        w7, _ = self.shape_record(QY7, level=2, rings=[True, False])
        w8, _ = self.shape_record(QY8, level=2, rings=[True, False])
        at = 22 + 2 * w7[22 + 2 * 6]
        self.assertEqual((w7[at + 3], w8[at + 3]), (0x7000, 0))
        self.assertEqual(w7[at + 9], 0x201e)

    def test_names_item_area_codes(self):
        for level in (1, 2):
            _, items = self.shape_record(QY7, level=level, area=(6, 2, 4))
            self.assertEqual(items[0][3:], [6, 0x0bc0, 246, 6, 2, 4])
        _, items = self.shape_record(QY8)
        self.assertEqual(items[0][6:], [0xe, 8, 0xd])

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
