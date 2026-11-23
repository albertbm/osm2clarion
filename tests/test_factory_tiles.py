# SPDX-License-Identifier: GPL-3.0-or-later
"""Readers against factory tiles. The tile cases skip without one:

    OSM2CLARION_FACTORY_TILE=B48R060R.DAT OSM2CLARION_QY7_TILE=B58R050R.DAT python3 -m unittest tests.test_factory_tiles
"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import clarion_rd, rd_check
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


if __name__ == '__main__':
    unittest.main()
