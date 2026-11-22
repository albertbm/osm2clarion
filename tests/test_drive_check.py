# SPDX-License-Identifier: GPL-3.0-or-later
"""drive_check: routes over a small graph, and what counts as a miss."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import card_check
import drive_check

PC = (3000, 29000)


def line(n=9):
    """A level-1 parcel with n nodes in a row, link k from node k - 1 to node k."""
    def node(k):
        ends = [dict(cls=0x0494, other=k + d, id=k + (d > 0), a=10, b=10) for d in (-1, 1) if 0 <= k + d < n]
        return dict(x=200 * k, y=100, nflags=0, entries=ends)
    return {PC: dict(nodes=[node(k) for k in range(n)])}


def tables(named, n=9):
    """Level-1 links 1..n-1 of class 4. named maps a link to its level-2 link id; a level-2
    link runs between the places of the level-1 links it covers."""
    shapes = {(PC, 4, k): (True, {2: (750, 7250, 4, named[k])} if k in named else {}) for k in range(1, n)}
    nodes = {2: {}, 4: {}, 5: {}}
    for k, up in named.items():
        nodes[2].setdefault((750, 7250, 4, up), set()).update({k - 1, k})
    drawn = {2: {key: None for key in nodes[2]}, 4: {}, 5: {}}
    walked = {2: {key: 'walked' for key in nodes[2]}, 4: {}, 5: {}}
    return shapes, drawn, walked, nodes


WHOLE = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}


class TestDrives(unittest.TestCase):
    def judge(self, shapes, drawn, walked, nodes):
        return drive_check.judge(drive_check.Graph(line()), shapes, drawn, walked, nodes, 20)

    def test_every_link_found_passes(self):
        r = self.judge(*tables(WHOLE))
        self.assertTrue(r['passed'], r)
        self.assertEqual(r['counts']['routes'], 20)
        self.assertEqual((r['counts']['L2 not drawn'], r['counts']['L2 not expanding']), (0, 0))
        self.assertGreater(r['counts']['L2 links driven'], 1)

    def test_a_level_1_link_with_no_shape_fails(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        del shapes[PC, 4, 5]
        r = self.judge(shapes, drawn, walked, nodes)
        self.assertFalse(r['passed'])
        self.assertIn('level 1 link class 4 id 5', r['notes'][0])
        self.assertIn('no shape', r['notes'][0])

    def test_an_upper_link_with_no_shape_fails(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        del drawn[2][750, 7250, 4, 2]
        r = self.judge(shapes, drawn, walked, nodes)
        self.assertFalse(r['passed'])
        self.assertIn('level 2 link class 4 id 2 in parcel (750, 7250)', r['notes'][0])

    def test_an_upper_link_that_does_not_expand_fails(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        walked[2][750, 7250, 4, 3] = 'backwards'
        r = self.judge(shapes, drawn, walked, nodes)
        self.assertFalse(r['passed'])
        self.assertIn('does not expand: backwards', r['notes'][0])

    def test_a_walk_off_the_loaded_tiles_is_no_miss(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        walked[2][750, 7250, 4, 3] = 'walk leaves the loaded tiles'
        self.assertTrue(self.judge(shapes, drawn, walked, nodes)['passed'])

    def test_a_stretch_no_upper_link_covers_fails(self):
        named = {k: v for k, v in WHOLE.items() if k != 5}
        named[6] = 5            # link 3 is gone, so the links either side of level-1 link 5 do not meet
        r = self.judge(*tables(named))
        self.assertFalse(r['passed'])
        self.assertTrue(any('a route falls off it' in n for n in r['notes']), r['notes'])
        self.assertEqual(r['counts']['L2 not expanding'], 1)

    def test_the_upper_link_across_the_stretch_is_driven_in_its_place(self):
        named = {k: v for k, v in WHOLE.items() if k != 5}
        named[6] = 5
        shapes, drawn, walked, nodes = tables(named)
        nodes[2][750, 7250, 4, 9] = {4, 5}      # a level-2 link no level-1 shape names
        drawn[2][750, 7250, 4, 9] = None
        self.assertTrue(self.judge(shapes, drawn, walked, nodes)['passed'])
        walked[2][750, 7250, 4, 9] = 'not named'
        r = self.judge(shapes, drawn, walked, nodes)
        self.assertFalse(r['passed'])
        self.assertTrue(any('id 9' in n and 'not named' in n for n in r['notes']), r['notes'])

    def test_routes_that_never_reach_a_level_fail(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        shapes = {k: (True, {}) for k in shapes}
        r = self.judge(shapes, drawn, walked, nodes)
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['L2 routes with no link'], 20)

    def test_a_parcel_that_did_not_load_is_not_judged(self):
        shapes, drawn, walked, nodes = tables(WHOLE)
        shapes = {(k[0], 4, k[2]): (True, {2: (751, 7250, 4, 1)} if k[2] == 5 else v[1]) for k, v in shapes.items()}
        self.assertTrue(self.judge(shapes, drawn, walked, nodes)['passed'])


class TestGraph(unittest.TestCase):
    def test_routes_are_long_and_run_link_by_link(self):
        g = drive_check.Graph(line())
        routes = g.routes(10)
        self.assertEqual(len(routes), 10)
        for start, end, path in routes:
            self.assertGreaterEqual(abs(start - end), 2)
            self.assertEqual(len(path), abs(start - end))
            self.assertEqual({link[0] for i, link in path}, {PC})

    def test_the_quickest_way_wins(self):
        L1 = line(3)
        L1[PC]['nodes'][0]['entries'].append(dict(cls=0x0494, other=2, id=7, a=5, b=99))
        L1[PC]['nodes'][2]['entries'].append(dict(cls=0x0494, other=0, id=7 | 0x2000, a=5, b=99))
        before = drive_check.Graph(L1).tree(0)
        self.assertEqual(before[2], (0, (PC, 4, 7)))

    def test_a_one_way_link_is_driven_its_own_way_only(self):
        L1 = line(3)
        L1[PC]['nodes'][0]['entries'].append(dict(cls=0x0494, other=2, id=7, a=5, b=99, w5=0x8000))
        L1[PC]['nodes'][2]['entries'].append(dict(cls=0x0494, other=0, id=7 | 0x2000, a=5, b=99, w5=0x4000))
        g = drive_check.Graph(L1)
        self.assertEqual(g.tree(0)[2], (1, (PC, 4, 2)))
        self.assertEqual(g.tree(2)[0], (2, (PC, 4, 7)))
        self.assertEqual(g.st['L1 link directions closed by one-way bits'], 1)

    def detour(self, word=None):
        """0-1-2 in a row, and 0-3-2 a little slower; word is a regulation word on node 1's
        entry towards 2."""
        L1 = line(3)
        nodes = L1[PC]['nodes']
        nodes[0]['entries'].append(dict(cls=0x0494, other=3, id=8, a=15, b=10))
        nodes[2]['entries'].append(dict(cls=0x0494, other=3, id=9 | 0x2000, a=15, b=10))
        nodes.append(dict(x=200, y=900, nflags=0, entries=[dict(cls=0x0494, other=0, id=8 | 0x2000, a=15, b=10),
                                                         dict(cls=0x0494, other=2, id=9, a=15, b=10)]))
        if word is not None:
            nodes[1]['entries'][1].update(w5=1 << 6, extra=[word])
        return drive_check.Graph(L1)

    def test_a_barred_turn_is_not_taken(self):
        self.assertEqual(self.detour().tree(0)[2], (1, (PC, 4, 2)))
        self.assertEqual(self.detour(0x00ff).tree(0)[2], (3, (PC, 4, 9)))
        self.assertEqual(self.detour(0xf0ff).tree(0)[2], (3, (PC, 4, 9)))

    def test_a_turn_barred_from_another_entry_stays_open(self):
        self.assertEqual(self.detour(0x10ff).tree(0)[2], (1, (PC, 4, 2)))

    def test_a_route_over_the_barred_turn_goes_round(self):
        g = self.detour(0x00ff)
        best, before = g.states(0)
        self.assertEqual([link[2] for i, link in g.path(before, g.ends(best)[2])], [8, 9])

    def test_the_layer_follows_the_class(self):
        self.assertEqual([drive_check.layer_of(c) for c in (0, 2, 3, 7, 8, 9, 10, 12)], [7, 7, 6, 6, 5, 4, 0, 0])

    def test_the_parcel_above(self):
        self.assertEqual(drive_check.parent(2, (3001, 29003), None), (750, 7250))
        self.assertEqual(drive_check.parent(4, (3001, 29003), (90, 900)), ((3001 - 2880) // 64, (29003 - 28800) // 64))


def two_parcels(w4, child=2):
    """Two level-2 parcels side by side, left node 0 crossing to right node 0 through one edge
    triple naming (cell 0x0000, w4); the right node's own block names (0x0000, child)."""
    left, right = (750, 7250), (750, 7251)
    L2 = {left: (dict(nodes=[dict(entries=[dict(cls=0xa000, other=0)])]),
                 dict(nodes=[dict(upper=0, parcel=0x00c0, child=0, entries=[(0, 0x0000, w4)])])),
          right: (dict(nodes=[dict(entries=[dict(cls=0xe000, other=0)])]),
                  dict(nodes=[dict(upper=0, parcel=0x0000, child=child, entries=[(0, 0x00c0, 0)])]))}
    L1 = {(3000, 29004): dict(nodes=[{}] * 3), (3000, 29003): dict(nodes=[{}])}
    return L1, L2, {left: 'B1', right: 'B2'}


class TestEdgeTriples(unittest.TestCase):
    def test_a_triple_naming_the_far_block_passes(self):
        st, per, notes = drive_check.edge_triples(*two_parcels(2))
        self.assertEqual((st['L2 edge triples'], st['L2 edge triples wrong'], notes), (2, 0, []))

    def test_a_triple_naming_the_far_level_2_index_is_wrong(self):
        st, per, notes = drive_check.edge_triples(*two_parcels(0))
        self.assertEqual((st['L2 edge triples wrong'], per['B1']['L2 edge triples wrong']), (1, 1))
        self.assertIn('the far level-2 index', notes[0])

    def test_a_child_past_the_level_1_record_is_reported(self):
        st, per, notes = drive_check.edge_triples(*two_parcels(5, 5))
        self.assertEqual((st['L2 edge triples wrong'], st['L2 edge triples child out of range']), (0, 1))
        self.assertIn('past the 3 nodes', notes[0])

    def test_a_wrong_triple_fails_the_drive(self):
        r = card_check.result('drives', True, {}, [])
        self.assertFalse(drive_check.add_edge_triples(r, *drive_check.edge_triples(*two_parcels(0)))['passed'])
        r = card_check.result('drives', True, {}, [])
        self.assertTrue(drive_check.add_edge_triples(r, *drive_check.edge_triples(*two_parcels(2)))['passed'])


class TestWalkOffTheTiles(unittest.TestCase):
    def test_a_walk_that_ends_at_an_unloaded_parcel_says_so(self):
        shapes = [((10, 10), (2048, 10)), ((2048 * 3, 10), (2048 * 3 + 5, 10))]
        self.assertEqual(card_check.walk_shapes(shapes, (10, 10)), 'walk stops short')
        self.assertEqual(card_check.walk_shapes(shapes, (10, 10), {(0, 0)}), 'walk leaves the loaded tiles')
        self.assertEqual(card_check.walk_shapes(shapes, (10, 10), {(0, 0), (0, 1)}), 'walk stops short')

    def test_a_shape_that_starts_a_few_units_on_is_a_stop(self):
        shapes = [((10, 10), (2048, 10)), ((2048, 13), (2060, 13))]
        self.assertEqual(card_check.walk_shapes(shapes, (10, 10), {(0, 0)}), 'walk stops short')


if __name__ == '__main__':
    unittest.main()
