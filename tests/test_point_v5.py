#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The X08-02 (QY7) POI search: records, the nearby leaf order and the whole-file layout."""
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))

import addre_io
import clarion_format
import point_v5

V5 = clarion_format.X08_02
# POINT032 on the 2014 QY7 card: its first FACILITY record, first INFORMATION record and first row
MUSEES = bytes.fromhex(
    '602c9f6fd43e0c2c044f06009f67d4360440135a054013550000000003010100000b080001ff001f000000000008ffff'
    '4c4553204d5553c94553204455205641544943414e00405649414c45205641544943414e4f00ffff')
TEL = bytes.fromhex('000a00082b283339292d30363633383833333300')
ROW = bytes.fromhex('001f00000000ffffffffffff00000029ffff090000000053')


def blank(size, cr=True):
    h = bytearray(size)
    if cr:
        h[0x32], h[0x33] = point_v5.CR // 2, point_v5.KW
    return h


def content(n=3, phones=True):
    """A small file's content: n facilities of two categories, a settlement and an ADM frame."""
    c = type('Content', (), {})()
    pre = bytearray(0x1100)
    pre[0x1032], pre[0x1033] = point_v5.CR // 2, point_v5.KW
    for i in range(300):
        struct.pack_into('>IH', pre, 0x800 + 6 * i, 0xffffffff, 0)
    struct.pack_into('>IH', pre, 0x800 + 6 * 6, addre_io.addr(0x1000), 1)
    c.prefix, c.df = bytes(pre), bytes(point_v5.DF_LEN)
    levels = (6, 6, 2, 4, 11)
    recs = [(b'KYLA', 31, 0xffff)] + [(b'PAIKKA %d' % i, 5 + i % 2, 3500 + i) for i in range(n)]
    c.names, c.infos = [], []
    for i, (nm, cat, sub) in enumerate(recs):
        tel = (0, len(c.infos)) if phones and cat != 31 else (0xffff, 0xffff)
        if tel[0] == 0:
            c.infos.append(point_v5.info_record(b'+358 40 %d' % i))
        c.names.append(point_v5.name_record(nm, levels, cat, sub, 0xaeaf, 0xdadf, 0x800 | (10 * i), 20 * i, tel=tel))
    c.keys = [(r[0], i, 1) for i, r in enumerate(recs)]
    c.rows = [point_v5.row(levels, i, cat, sub) for i, (nm, cat, sub) in enumerate(recs)]
    cats = sorted({r[1] for r in recs})
    c.cats = [((6, k), [(recs[i][0], j, 1) for j, i in enumerate(x for x in range(len(recs)) if recs[x][1] == k)],
               [c.rows[i] for i in range(len(recs)) if recs[i][1] == k]) for k in cats]
    subs = sorted({(r[1], r[2]) for r in recs if r[2] != 0xffff})
    c.subs = [((6,) + cs, [(recs[i][0], 0, 1)], [c.rows[i]]) for cs in subs
              for i in [next(x for x in range(len(recs)) if recs[x][1:] == cs)]]
    c.near = []
    for k in cats:
        items = [(i, k, recs[i][2], 0xaeaf, 0xdadf, 0x800 | (10 * i), 20 * i, 63, recs[i][0])
                 for i in range(len(recs)) if recs[i][1] == k]
        c.near.append(((0,) * 11 + (k,), items, any(it[2] != 0xffff for it in items)))
    head = blank(0x100)
    struct.pack_into('>H', head, 0x3a, 6)
    c.adm_head, c.adm_df = bytes(head), bytes(0x100)
    adm_name = struct.pack('>HBBBB5HH', 11, 5, 1, 2, 0xff, *levels, 21) + b'KYLA'
    c.adm_names = [adm_name]
    c.adm_keys = [(b'KYLA', 0, 1)]
    c.adm_rows = [struct.pack('>5HHHI', *levels, 0xffff, 0, 0)]
    return c


class Records(unittest.TestCase):
    def test_factory_record(self):
        f = point_v5.parse_name_record(MUSEES)
        self.assertEqual((point_v5.text(f['name']), f['category'], f['refs']),
                         ('LES MUSÉES DU VATICAN'.encode('latin-1'), 8, ((4, 64, 4954), (5, 64, 4949))))
        self.assertEqual(point_v5.name_record(**f), MUSEES)

    def test_info_and_row(self):
        self.assertEqual(point_v5.info_record(b'+(39)-066388333'), TEL)
        self.assertEqual(point_v5.row(**point_v5.parse_row(ROW)), ROW)
        self.assertEqual(point_v5.parse_row(ROW)['lang'], 9)


class Nearby(unittest.TestCase):
    def test_leaf_order(self):
        # x first, then y, code and the Latin-1 name: Ö sorts after R, not with O
        items = [(0, 1, 0xffff, 0xaeaf, 0xdadf, 0x800 | 5, 9, 63, 'JOENSUUN ÖLJY'.encode('latin-1')),
                 (1, 1, 0xffff, 0xaeaf, 0xdadf, 0x800 | 5, 9, 63, b'JOENSUUN RUOSTE'),
                 (2, 1, 0xffff, 0xaeaf, 0xdadf, 0x800 | 4, 700, 63, b'ZETA')]
        out = point_v5.nearby_frame(items)
        got, sub = point_v5.parse_nearby(out, 0, 1)
        self.assertFalse(sub)
        self.assertEqual([it[0] for it in got], [2, 1, 0])


class Layout(unittest.TestCase):
    def test_reads_back(self):
        c = content()
        out = point_v5.assemble(c, V5.deflate)
        p = point_v5.Point(out)
        self.assertEqual((p.keys, p.rows, p.names, p.infos), (c.keys, c.rows, c.names, c.infos))
        self.assertEqual([(t, k, r) for t, k, r in p.cats], c.cats)
        self.assertEqual([(t, k, r) for t, k, r in p.subs], c.subs)
        self.assertEqual((p.adm_keys, p.adm_rows, p.adm_names), (c.adm_keys, c.adm_rows, c.adm_names))
        self.assertEqual([[it[0] for it in items] for _w, items, _s in p.near],
                         [[it[0] for it in items] for _w, items, _s in c.near])

    def test_definition_fields(self):
        c = content()
        p = point_v5.Point(point_v5.assemble(c, V5.deflate))
        self.assertEqual(struct.unpack_from('>H', p.df, 0x3a)[0], max(len(r) for r in c.names) // 2)
        self.assertEqual(p.df[0x55], 5)
        self.assertEqual(p.df[0x17f], max(r[3] for r in c.infos))
        self.assertEqual(p.adm_df[0x47], 5)

    def test_no_sub_categories(self):
        c = content()
        c.subs = []
        out = point_v5.assemble(c, V5.deflate)
        self.assertEqual(struct.unpack_from('>IH', out, 0x800 + 6 * 279), (0xffffffff, 0))


if __name__ == '__main__':
    unittest.main()
