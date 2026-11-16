#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Coarse road shapes, chained level-2 shapes, edge twins across parcels, and the region
road selector. Inputs are built in code; nothing here needs a card or an OSM extract."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

import clarion_profile
try:
    import lev_roads                     # lev_coarse needs shapely
except ImportError:
    lev_roads = None
import fa_encode
import osm_roads
import rd_build


@unittest.skipIf(lev_roads is None, 'shapely is not installed')
class TestCoarseShapes(unittest.TestCase):
    # factory words off the 2015 coarse file: named and unnamed level-4 shapes, a level-3 shape
    L4_NAMED = [0x0010, 0x8004, 0x6000, 0x0000, 0x000c, 0x0000, 0x0000, 0x0d01, 0x08cc, 0x0001,
                0x3cf9, 0x00d5, 0x1cf1, 0x00e8, 0x14ef, 0x0121]
    L4_TWO = [0x000f, 0x8003, 0x6000, 0x1000, 0x000c, 0x0000, 0x0000, 0x0d01, 0x88cc, 0x0002,
              0x2501, 0x88cc, 0x0003, 0x3501, 0x80cc]
    L3 = [0x000d, 0x0003, 0x7000, 0x0000, 0x000c, 0x0000, 0x0000, 0x0967, 0x0372, 0x016d, 0x038e,
          0x1167, 0x0388]

    def test_factory_shapes_round_trip(self):
        for words, level in ((self.L4_NAMED, 4), (self.L4_TWO, 4), (self.L3, 3)):
            s = lev_roads.decode(words, level)
            self.assertIsNotNone(s, words)
            self.assertEqual(lev_roads.encode(s, level), words)

    def test_link_end_inside_a_level4_shape(self):
        s = lev_roads.decode(self.L4_TWO, 4)
        self.assertEqual([lid for lid, _ in s['links']], [2, 3])
        self.assertEqual([len(p) for _, p in s['links']], [1, 1])

    def test_broken_flags_are_refused(self):
        bad = list(self.L4_NAMED)
        bad[12] &= ~0x0800
        self.assertIsNone(lev_roads.decode(bad, 4))

    def test_shape_writes_the_named_form(self):
        w = lev_roads.shape(4, 6, [[(10, 20), (30, 40), (50, 60)]], name_off=12)
        self.assertEqual(w[1], 0x8003)
        self.assertEqual((w[2], w[3], w[4]), (0x6000, 0, 12))
        self.assertEqual(w[7:9], [0x0800 | 10, 0x0800 | 20], 'start, y says a link header follows')
        self.assertEqual(w[9], 1, 'the link id')
        self.assertEqual(w[10] & 0xf800, 0x3800, 'first point of the link, goes on')
        self.assertEqual(w[12] & 0xf800, 0x1000, 'the last point')
        self.assertIsNotNone(lev_roads.decode(w, 4))

    def test_level4_shape_names_its_level5_link(self):
        w = lev_roads.shape(4, 4, [[(10, 20), (30, 40)]], name_off=12, ids=[7], upper=3)
        self.assertEqual(w[0] >> 13, 1, 'one more header word')
        self.assertEqual(w[5:8], [0, 0x8000 | 3, 0], 'the factory place: after word 5')
        self.assertEqual(w[8:11], [0x0800 | 10, 0x0800 | 20, 7], 'start and link id follow')
        self.assertIsNotNone(lev_roads.decode(w, 4))

    def test_direction_goes_on_every_y(self):
        along = lev_roads.shape(4, 4, [[(10, 20), (30, 40), (50, 60)]], name_off=12, ids=[7], direction=1)
        self.assertEqual(along[3], 0x1000, 'named and one-way')
        self.assertEqual([y & 0xc000 for y in (along[8], along[11], along[13])], [0x4000] * 3)
        against = lev_roads.shape(4, 4, [[(10, 20), (30, 40)]], ids=[7], direction=-1)
        self.assertEqual((against[3], against[8] & 0xc000), (0x3000, 0x8000))
        both = lev_roads.shape(4, 7, [[(10, 20), (30, 40)]], ids=[7], direction=0)
        self.assertEqual((both[3], both[8] & 0xc000, both[11] & 0xc000), (0, 0, 0), 'unnamed and two-way')
        self.assertIsNotNone(lev_roads.decode(along, 4))

    def test_layer_keeps_a_class_together(self):
        shapes = [lev_roads.shape(4, c, [[(0, 0), (9, 9)]], ids=[i]) for i, c in enumerate((5, 3, 7, 3, 10, 5), 1)]
        order = [(sh[2] >> 12, sh[-3]) for sh in lev_roads.by_layer(shapes)]
        self.assertEqual(order, [(10, 5), (3, 2), (3, 4), (5, 1), (5, 6), (7, 3)], 'layer 0 first, ids in step')

    def test_level3_has_no_link_ids(self):
        w = lev_roads.shape(3, 7, [[(1, 2), (3, 4), (5, 6)]])
        self.assertEqual(w[1], 3, 'level 3 counts points without 0x8000')
        self.assertEqual(w[3], 0x3000, 'unnamed')
        self.assertEqual(len(w), 7 + 6)
        self.assertIsNotNone(lev_roads.decode(w, 3))

    def test_record_and_road_block(self):
        shapes = [lev_roads.shape(4, 4, [[(0, 0), (100, 100)]], 12)]
        plain = lev_roads.record((0x9400, 0xc600), shapes, 352)
        w = struct.unpack('>%dH' % (len(plain) // 2), plain)
        self.assertEqual((w[2], w[7], w[20]), (0x00fa, 11, 352))
        g = 2 * w[7]
        self.assertEqual((w[g + 12], w[g + 13]), (11, 1), 'layer 6: data at dword 11, one shape')
        self.assertEqual(list(w[g + 22:g + 22 + len(shapes[0])]), shapes[0])

    def test_chains_join_one_route(self):
        a = dict(id=1, hw='trunk', ref='1', nodes=[(0, 0), (1, 0)])
        b = dict(id=2, hw='trunk', ref='1', nodes=[(2, 0), (1, 0)])
        c = dict(id=3, hw='primary', ref='41', nodes=[(1, 0), (1, 1)])
        runs = lev_roads.chains([a, b, c])
        self.assertEqual(sorted(len(r['nodes']) for r in runs), [2, 3])


class TestLevel2Chains(unittest.TestCase):
    NODES = [(100, 100), (300, 120), (500, 300)]

    def test_ring_marker_in_header_word(self):
        chain = [(1, 0, 1, []), (2, 1, 2, [])]
        plain = fa_encode.shape_words(chain, self.NODES, 12, level=1, cls=8)
        ring = fa_encode.shape_words(chain, self.NODES, 12, level=1, cls=8, ring=True)
        self.assertEqual((plain[3], ring[3]), (0x0004, 0x7004))
        self.assertEqual(plain[:3] + plain[4:], ring[:3] + ring[4:], 'only word 3 changes')
        self.assertEqual(fa_encode.shape_words(chain, self.NODES, 12, level=2, cls=7, ring=True)[3], 0x7000,
                         'level 2 carries the code too, without the level-1 constant')


    def test_two_links_with_bends(self):
        chain = [(1, 0, 1, [(200, 110)]), (2, 1, 2, [(400, 200)])]
        w = fa_encode.shape_words(chain, self.NODES, 12, level=2, cls=7)
        self.assertEqual(w[1], 0x8005)
        self.assertEqual(w[7:11], [0x0800 | 100, 0x2800 | 100, 0x101e, 1], 'start, style, first id')
        self.assertEqual(w[11] & 0xf800, 0x5800, 'first point of the styled link')
        self.assertEqual((w[13] & 0xf800, w[14] & 0xf800), (0, 0x0800), 'link end inside the shape')
        self.assertEqual(w[15], 2, 'the second link has no style word')
        self.assertEqual(w[16] & 0xf800, 0x3800, 'first point of an unstyled link')
        self.assertEqual(w[18] & 0xf800, 0x1000, 'the last point')
        self.assertEqual(len(w), w[0])

    def test_rd_build_chains_level2(self):
        ways = [dict(id=1, hw='residential', name='A', ref=None,
                     nodes=[(-21.900 + 0.002 * i, 64.130 + 0.0004 * i) for i in range(5)])]
        parcels = rd_build.build_graphs(ways, 'B60R080R', 2, True)
        P = next(iter(parcels.values()))
        self.assertEqual(len(P['links']), 1)
        self.assertEqual(len(P['links'][0][8]), 3, 'the bends ride inside the level-2 link')

    def test_level2_keeps_both_sides_of_an_island(self):
        p, q = (-21.900, 64.130), (-21.899, 64.130)
        east = dict(id=1, hw='secondary', name='A', ref=None, dir=1, nodes=[p, q])
        west = dict(id=2, hw='secondary', name='A', ref=None, dir=1, nodes=[q, p])
        self.assertEqual(len(next(iter(rd_build.build_graphs([east, west], 'B60R080R', 2, True).values()))['links']), 1)
        west['full'] = [q, (-21.8995, 64.1301), p]
        links = next(iter(rd_build.build_graphs([east, west], 'B60R080R', 2, True).values()))['links']
        self.assertEqual(sorted(l[3] for l in links), [1, 2, 2], 'the side simplify left no point is cut at one it took')


    def test_level2_keeps_junctions_of_one_unit_apart(self):
        a, b, c, d = (-21.9020, 64.1300), (-21.90000, 64.13000), (-21.899995, 64.13001), (-21.8980, 64.1310)
        pc = rd_build.parcel_of(*b, 2)
        self.assertEqual(rd_build.quant(b, pc, 2), rd_build.quant(c, pc, 2))
        ways = [dict(id=1, hw='trunk', name='A', ref='1', nodes=[a, b]),
                dict(id=2, hw='primary', name='B', ref='47', nodes=[b, c]),
                dict(id=3, hw='trunk', name='A', ref='1', nodes=[c, d]),
                dict(id=4, hw='primary', name='B', ref='47', nodes=[c, (-21.8990, 64.1290)]),
                dict(id=5, hw='primary', name='B', ref='47', nodes=[b, (-21.9010, 64.1310)])]
        P = next(iter(rd_build.build_graphs(ways, 'B60R080R', 2, True).values()))
        geos = sorted(tuple(nd['geo']) for nd in P['nodes'].values())
        self.assertEqual(len(geos), 6, 'two junctions 2 m apart are two level-2 nodes')
        self.assertIn(b, geos); self.assertIn(c, geos)
        self.assertEqual(sorted(l[3] for l in P['links']), [1, 2, 3, 4, 5], 'the link between them is kept')

    def test_level2_keeps_a_dip_over_its_parcel_line(self):
        line = 772 / 12
        w = dict(id=1, hw='primary', name='A', ref='47',
                 nodes=[(-21.720, line - 0.0003), (-21.717, line + 0.00001), (-21.714, line - 0.0003)])
        pinned = rd_build.pin_crossings([w])
        self.assertIn((-21.717, line + 0.00001), pinned)
        w['nodes'] = rd_build.simplify(w['nodes'], 5, pinned)
        parcels = rd_build.build_graphs([w], 'B60R080R', 2, True)
        self.assertEqual(sorted(len(P['links']) for P in parcels.values()), [1, 2], 'a level-2 link over the line too')

    def test_a_secondary_without_a_number_is_class_6(self):
        self.assertEqual(fa_encode.road_kind('secondary', '34'), 'secondary')
        self.assertEqual(fa_encode.road_kind('primary', None), 'primary')
        for level in (1, 2):
            self.assertEqual(fa_encode.layer_class(fa_encode.road_kind('secondary', None), level), (6, 6))
            self.assertEqual(fa_encode.layer_class(fa_encode.road_kind('secondary_link', ''), level), (6, 6))


class TestEdgeTwins(unittest.TestCase):
    def setUp(self):
        rd_build.use_profile(clarion_profile.load(str(Path(__file__).resolve().parent.parent
                                                      / 'profiles' / 'iceland.toml')))

    def test_every_edge_entry_is_answered(self):
        lon_edge = -22.0 + 1.875 / 60 * 3
        a = dict(id=1, hw='residential', name='A', ref=None,
                 nodes=[(lon_edge - 0.01, 64.130), (lon_edge + 0.01, 64.131)])
        b = dict(id=2, hw='residential', name='B', ref=None,
                 nodes=[(lon_edge - 0.01, 64.1352), (lon_edge + 0.0000001, 64.1353), (lon_edge - 0.01, 64.1354)])
        parcels = rd_build.build_graphs([a, b], 'B60R080R', 1, True)
        recs = rd_build.encode_parcels(parcels, 1)
        import rd_check
        n, bad = rd_check.edge_joins([zlib_plain(r) for r in recs.values()], 1)
        self.assertGreater(n, 0)
        self.assertEqual(bad, [])

    def test_a_crossing_beside_a_corner_keeps_its_twin(self):
        lon, lat, u = -22.0 + 1.875 / 60 * 3, 64.125, 1.875 / 60 / 2048
        w = dict(id=1, hw='secondary', name='A', ref='26',
                 nodes=[(lon - 0.3 * u - 0.001, lat - 0.002), (lon - 0.3 * u + 0.001, lat + 0.002)])
        parcels = rd_build.build_graphs([w], 'B60R080R', 1, True)
        self.assertEqual(sum(len(P['links']) for P in parcels.values()), 3, 'the clipped parcel keeps its piece')
        import rd_check
        n, bad = rd_check.edge_joins([zlib_plain(r) for r in rd_build.encode_parcels(parcels, 1).values()], 1)
        self.assertEqual((n, bad), (4, []))

    def test_ring_parcels_are_built_but_not_written(self):
        x0 = -28.0
        w = dict(id=1, hw='residential', name='A', ref=None, nodes=[(x0 + 0.01, 64.13), (x0 - 0.01, 64.13)])
        parcels = rd_build.build_graphs([w], 'B60R080R', 1, True)
        self.assertEqual(sorted(P['ring'] for P in parcels.values()), [False, True])
        self.assertEqual(len(rd_build.encode_parcels(parcels, 1)), 1)


def zlib_plain(rec):
    import zlib
    return zlib.decompress(rec[1])


class TestAreaRoads(unittest.TestCase):
    def test_keep(self):
        k = osm_roads.keep
        self.assertIsNone(k({'highway': 'residential'}))
        self.assertIsNone(k({'highway': 'service', 'name': 'Skúlagata'}))
        self.assertEqual(k({'highway': 'service'}), 'service, unnamed')
        self.assertEqual(k({'highway': 'service', 'name': 'X', 'service': 'parking_aisle'}), 'service=parking_aisle')
        self.assertEqual(k({'highway': 'footway'}), 'not a road class')
        self.assertEqual(k({'highway': 'residential', 'access': 'private'}), 'closed to cars')

    def test_oneway(self):
        self.assertEqual(osm_roads.oneway({'oneway': 'yes'}), 1)
        self.assertEqual(osm_roads.oneway({'oneway': '-1'}), -1)
        self.assertEqual(osm_roads.oneway({'junction': 'roundabout'}), 1)
        self.assertEqual(osm_roads.oneway({}), 0)
        self.assertEqual(osm_roads.oneway({'oneway': 'true'}), 1)
        self.assertEqual(osm_roads.oneway({'junction': 'roundabout', 'oneway': 'no'}), 0)
        self.assertEqual(osm_roads.oneway({'highway': 'motorway'}), 1)
        self.assertEqual(osm_roads.oneway({'highway': 'motorway_link', 'oneway': 'no'}), 0)
        self.assertEqual(osm_roads.oneway({'highway': 'trunk', 'oneway': 'reversible'}), 0)

    def test_oneway_dir_is_the_shared_function(self):
        d = osm_roads.oneway_dir
        self.assertEqual(d('-1', None, None), -1)
        self.assertEqual(d(None, 'roundabout', None), 1)
        self.assertEqual(d(None, None, 'motorway'), 1)
        self.assertEqual(d('no', 'roundabout', None), 0, 'an explicit no overrides the roundabout')
        self.assertEqual(d(True, None, None), 1, 'a legacy collapsed bool reads as oneway=yes/absent')
        self.assertEqual(d(False, 'roundabout', None), 0, 'same legacy bool loses the roundabout implication')
        self.assertEqual(fa_encode.style_word(50, oneway=True), 0x0029)

    def test_clip_keeps_points_and_adds_crossings(self):
        inside = lambda p: p[0] < 10
        cross = lambda a, b: (10.0, a[1] + (b[1] - a[1]) * (10 - a[0]) / (b[0] - a[0]))
        pts = [(0, 0), (5, 0), (15, 0), (20, 0), (5, 5), (6, 5)]
        runs = osm_roads.clip(pts, inside, cross)
        self.assertEqual(runs[0], [(0, 0), (5, 0), (10.0, 0.0)])
        self.assertEqual(runs[1][0][0], 10.0)
        self.assertEqual(runs[1][1:], [(5, 5), (6, 5)])
        self.assertEqual(osm_roads.clip([(11, 0), (12, 0)], inside, cross), [])


class TestCollapse(unittest.TestCase):
    def test_merge_stops_at_the_longest_length_word(self):
        import lev_graph
        def link(a, b):
            return dict(a=a, b=b, length=60000.0, cls=1, speed=90.0, direction=0, parcel=(0, 0),
                        child=(0, 0), next_a=b, next_b=a, pa=(0, 0), pb=(0, 0))
        per = {(0, 0): [link(1, 2), link(2, 3), link(3, 4)]}
        lev_graph.collapse(per)
        self.assertEqual(sorted(l['length'] for l in per[(0, 0)]), [60000.0, 120000.0])


if __name__ == '__main__':
    unittest.main()
