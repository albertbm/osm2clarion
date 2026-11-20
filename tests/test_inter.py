#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The X08-02 (QY7) intersection search: records, keys, the file layout and the builder."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))

import addre_io
import build_inter
import clarion_bg
import clarion_format
import inter_io

V5 = clarion_format.X08_02
# INTER032 on the 2014 QY7 card: its first 1ST-STREET record and the crossing it points at
PIAZZA = bytes.fromhex('a012000000000001030a001f000000015049415a5a4120444f4e204d4f524f53494e4900')
SALITA = bytes.fromhex('80139f4fd45c06ba05bf030a001f0000000153414c4954412053414e5427414e544f4e494f00')
ROW = bytes.fromhex('001f00000001ffffffffffff000000000001')


def prefix():
    """A header and frame with the fields assemble sets left zero."""
    h = bytearray(0x1100)
    h[0x1032], h[0x1033] = inter_io.CR // 2, inter_io.KW
    for i in range(300):
        struct.pack_into('>IH', h, 0x800 + 6 * i, 0xffffffff, 0)
    struct.pack_into('>IH', h, 0x800 + 6 * 6, addre_io.addr(0x1000), 1)
    struct.pack_into('>IH', h, 0x800 + 6 * 7, 0, 2)
    return bytes(h)


class Records(unittest.TestCase):
    def test_factory_records(self):
        f = inter_io.parse_first(PIAZZA)
        self.assertEqual((f['name'], f['levels'], f['second_at'], f['count']), ('PIAZZA DON MOROSINI', (31, 0, 1), 0, 1))
        self.assertEqual(inter_io.first_record(f['name'], f['levels'], f['second_at'], f['count']), PIAZZA)
        s = inter_io.parse_second(SALITA)
        self.assertEqual((s['x'], s['y'], s['more']), (1722, 1471, False))
        self.assertEqual(inter_io.second_record(s['name'], s['levels'], s['la'], s['lo'], s['x'], s['y'], s['more']), SALITA)
        self.assertEqual(inter_io.row(**inter_io.parse_row(ROW)), ROW)
        self.assertEqual(inter_io.parse_row(ROW)['levels'][:3], (31, 0, 1))

    def test_run_flag(self):
        r = inter_io.second_record('TIE', (6, 6, 2, 4, 11), 1, 2, 3, 4, True)
        self.assertEqual(r[0] >> 3, inter_io.SECOND_MORE)


class Keys(unittest.TestCase):
    def test_fold(self):
        self.assertEqual(inter_io.search_key('GREGORIUS IX:N TIE'), 'GREGORIUS IX N TIE')
        self.assertEqual(inter_io.search_key('AHVENVAARA (ÖLLÖLÄ)'), 'AHVENVAARA OLLOLA')
        self.assertEqual(inter_io.search_key('JOKIPELTO(OTALAMPI)'), 'JOKIPELTO OTALAMPI')

    def test_no_one_letter_word(self):
        keys = inter_io.word_keys(inter_io.search_key('A. F. AIRON KATU'))
        self.assertEqual(keys, [(b'A F AIRON KATU', False), (b'AIRON KATU', True), (b'KATU', True)])
        self.assertEqual(inter_io.word_keys('PAJUUSTONTIE 1'), [(b'PAJUUSTONTIE 1', False)])


class Layout(unittest.TestCase):
    def build(self, n=3):
        levels = (6, 6, 2, 4, 11)
        firsts, seconds, keys, rows = [], [], [], []
        for i in range(n):
            name = 'KATU %05d' % i
            firsts.append(inter_io.first_record(name, levels, 2 * i, 2))
            seconds += [inter_io.second_record('TIE A', levels, 0xaeaf, 0xdadf, 5, 6, False),
                        inter_io.second_record('TIE B', levels, 0xaeaf, 0xdadf, 7, 8, True)]
            keys.append((name.encode('ascii'), i, 1))
            rows.append(inter_io.row(levels, i))
        df = bytes(inter_io.DF_LEN)
        out = inter_io.assemble(prefix(), df, keys, rows, firsts, seconds, 6, V5.deflate)
        return out, keys, rows, firsts, seconds

    def test_reads_back(self):
        out, keys, rows, firsts, seconds = self.build()
        I = inter_io.Inter(out)
        self.assertEqual((I.keys, I.rows, I.first, I.second, I.area), (keys, rows, firsts, seconds, 6))
        self.assertEqual((I.per1, I.per2), (inter_io.PER1, inter_io.PER2))

    def test_definition_fields(self):
        out, _keys, _rows, firsts, seconds = self.build()
        I = inter_io.Inter(out)
        self.assertEqual(I.df[inter_io.DF_N1[3]], 5)
        self.assertEqual(I.df[inter_io.DF_N1[4]], max(r[9] for r in firsts))
        self.assertEqual(struct.unpack_from('>H', I.df, inter_io.DF_N2[2])[0], max(len(r) for r in seconds) // 2)

    def test_plain_rows(self):
        levels = (39, 0, 1)
        firsts = [inter_io.first_record('PIAZZA', levels, 0, 1, 'utf-8')]
        seconds = [inter_io.second_record('SALITA', levels, 0x9f4f, 0xd45c, 1722, 1472, False, 'utf-8')]
        rows = [inter_io.row(levels, 0)]
        out = inter_io.assemble(prefix(), bytes(inter_io.DF_LEN), [(b'PIAZZA', 0, 1)], rows, firsts, seconds, 39,
                                addre_io.deflate_block, packed=False)
        I = inter_io.Inter(out)
        self.assertFalse(I.packed)
        self.assertEqual((I.rows, I.first, I.second), (rows, firsts, seconds))

    def test_many_blocks(self):
        out, _keys, _rows, firsts, seconds = self.build(1700)
        I = inter_io.Inter(out)
        self.assertEqual((I.first, I.second), (firsts, seconds))


class Colon(unittest.TestCase):
    def test_a_street_gets_its_colon_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'roads.json'
            p.write_text(json.dumps([dict(name='Gregorius IX:n tie'), dict(name='Yrjönkatu'), dict(name='Tie "A"')]))
            self.assertEqual(build_inter.colon_names(p, lambda s: s), {"GREGORIUS IX'N TIE": 'GREGORIUS IX:N TIE'})


class Builder(unittest.TestCase):
    def test_one_crossing_per_pair(self):
        la, lo = clarion_bg.parcel_code(1, 64 * 48, 950 * 32)
        nodes = {(la, lo, 10, 20): {'ATIE', 'BTIE'}, (la, lo, 30, 40): {'ATIE', 'BTIE'},
                 (la, lo, 50, 60): {'ATIE'}, (la, lo, 70, 80): {'ATIE', 'CTIE', 'ÞTIE'}}
        keys, rows, firsts, seconds, st = build_inter.build(nodes, lambda name, p: ((6, 6, 2, 4, 11), 'name'), 'latin-1')
        f = [inter_io.parse_first(r) for r in firsts]
        # ÞTIE has no key the keyboard can type, so it is a crossing only
        self.assertEqual([x['name'] for x in f], ['ATIE', 'BTIE', 'CTIE'])
        self.assertEqual(st['skipped'], ['ÞTIE'])
        a = [inter_io.parse_second(r) for r in seconds[f[0]['second_at']:f[0]['second_at'] + f[0]['count']]]
        self.assertEqual([(s['name'], s['x'], s['y'], s['more']) for s in a],
                         [('BTIE', 10, 20, False), ('CTIE', 70, 80, True), ('ÞTIE', 70, 80, True)])
        self.assertEqual(len(rows), 3)
        self.assertEqual([k[0] for k in keys], [b'A', b'B', b'C'])


if __name__ == '__main__':
    unittest.main()
