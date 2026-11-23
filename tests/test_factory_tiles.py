# SPDX-License-Identifier: GPL-3.0-or-later
"""Readers against factory tiles. The tile cases skip without one:

    OSM2CLARION_FACTORY_TILE=B48R060R.DAT OSM2CLARION_QY7_TILE=B58R050R.DAT python3 -m unittest tests.test_factory_tiles
"""
import os, struct, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import clarion_format, clarion_rd, clarion_tile, rd_check
from tile_scan import TileScan

QY8_ENV, QY7_ENV = 'OSM2CLARION_FACTORY_TILE', 'OSM2CLARION_QY7_TILE'


def tiles():
    return [os.environ[e] for e in (QY8_ENV, QY7_ENV) if os.environ.get(e)]


def needs_tile(env=None):
    have = os.environ.get(env) if env else tiles()
    return unittest.skipUnless(have, 'set %s to a factory tile' % (env or QY8_ENV + ' or ' + QY7_ENV))


def entry(w4, w5, eid, extra):
    return dict(other=0, w4=w4, w5=w5, id=eid, cls=0x0898, a=1, b=1, extra=extra)


class LinkSize(unittest.TestCase):
    def test_wide_counts_frame(self):
        """66 regulation words and 17 more need w5 bits 12-13 and 4-5; w4 bit 0 and the id's top bits add words too."""
        ents = [entry(0, 66 << 6 | 17, 1, list(range(83))), entry(1, 0, 0x4002, [7, 8]), entry(0, 0, 3, [])]
        node = dict(x=5, y=6, xflags=0, yflags=0, ncls=8, nflags=0, entries=ents)
        rec = dict(code=(0x8000, 2), pairs=[(27, 0)] + [(0xffffffff, 0)] * 4, units={0: []}, nnodes=1, nlinks=3,
                   classes=[(0, 0)] * 11, nodes=[node], countries=[352], trailer=[])
        rec['pairs'][0] = (27, len(clarion_rd.unit0_words(rec)))
        back = clarion_rd.parse_record(clarion_rd.encode_record(rec))
        self.assertNotIn('error', back)
        self.assertEqual(back['consumed'], back['unit_len'])
        self.assertEqual([e['extra'] for e in back['nodes'][0]['entries']], [e['extra'] for e in ents])
        self.assertEqual(clarion_rd.reg_count(ents[0]['w5']), 66)

    @needs_tile()
    def test_every_factory_record_frames(self):
        for path in tiles():
            with TileScan(path) as t:
                for level in (1, 2):
                    n = bad = 0
                    for r in t.records(section=2, level=level):
                        rec = clarion_rd.parse_record(rd_check.part_a(r.plain(), level))
                        if 'nnodes' not in rec: continue
                        n += 1
                        bad += bool(rec.get('error')) or len(rec['nodes']) != rec['nnodes'] or rec['consumed'] != rec['unit_len']
                    self.assertGreater(n, 0, path)
                    self.assertEqual(bad, 0, '%s level %d' % (path, level))


class ExtendedSlots(unittest.TestCase):
    def test_flagged_pair_and_its_extra_record(self):
        """An extended slot: the road pair has bit 31, the next two pairs size one extra record."""
        fmt = clarion_format.X08_02
        main, sub = fmt.deflate(b'ab' * 40), fmt.deflate(b'cd' * 12)
        body = struct.pack('>II', 0x80000000 | len(main) // 2, 40)
        body += struct.pack('>4H', len(sub) // 2, 0xffff, 0xffff, 0xffff) + struct.pack('>4H', 12, 0, 0, 0)
        body += b'\xff' * (1024 - len(body))
        block = 0x1000
        d = bytes(block) + main + sub
        head = struct.pack('>6H', 0x2146, 32, 32, 90, 30, 0x92)
        d = head + struct.pack('>I', clarion_tile.enc_addr(block)) + body + d[12 + 4 + 1024:]
        D = clarion_tile.read_dir(d, 0, box=(0, 0, 0, 0), nslots=1, section=1, typ=0x92, fmt=fmt, wide=True)
        s = D['slots'][0]
        self.assertTrue(s['ext'])
        self.assertEqual([(r['k'], r.get('sub', 0), r['raw']) for r in s['recs']], [(0, 0, main), (0, 1, sub)])
        self.assertEqual(clarion_tile.dir_size(D), 12 + 4 + 1024)
        self.assertEqual(clarion_tile.dir_bytes(D, {0: block}), d[:12 + 4 + 1024])

    @needs_tile()
    def test_factory_tile_round_trips(self):
        for path in tiles():
            with open(path, 'rb') as f: d = f.read()
            self.assertTrue(clarion_tile.write_tile(clarion_tile.read_tile(d)) == d, path)


if __name__ == '__main__':
    unittest.main()
