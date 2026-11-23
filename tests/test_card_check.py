#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""card_check.py's rules against hand-built records: no card, no OSM extract, nothing but
the standard library. The records are minimal dicts shaped like clarion_rd.parse_record's
output, not real encoded bytes - card_check only ever reads that shape, so this is enough
to catch a rule crashing or flipping its pass/fail sense without needing a real tile."""
import collections
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

import card_check
import clarion_format


def node(ncls=0, x=0, y=0, nflags=0, entries=()):
    return dict(ncls=ncls, x=x, y=y, nflags=nflags, entries=list(entries))


def entry(other=0, w4=0, w5=0, id=0, cls=0, a=0, b=0, extra=()):
    return dict(other=other, w4=w4, w5=w5, id=id, cls=cls, a=a, b=b, extra=list(extra))


class TestEmptyInputs(unittest.TestCase):
    """Every rule must pass vacuously, not crash, on an empty tile set."""

    def test_run_all_on_nothing(self):
        checks = card_check.run_all({}, {}, {}, {}, {})
        self.assertEqual(len(checks), 23)
        for c in checks:
            self.assertTrue(c['passed'], c['name'])


class TestL2TripleW4(unittest.TestCase):
    def cards(self, w2, w4):
        # a level-2 node whose child is level-1 node 0 of parcel (0, 0), an edge node on its east line
        child = node(x=2047, y=500, entries=[entry(other=1, id=1, cls=0x0497), entry(other=3, cls=0xa797)])
        L1 = {(0, 0): dict(nodes=[child, node(x=1900, y=500)]),
              (0, 1): dict(nodes=[node(), node(), node(), node(x=0, y=500), node(x=100, y=500)])}
        a = dict(nodes=[node(entries=[entry(other=1, id=1, cls=0x0497)]), node()])
        b = dict(nodes=[dict(upper=0, parcel=0, child=0, entries=[(1, w2, w4)])])
        return L1, {(0, 0): (a, b)}

    def test_a_neighbour_in_the_own_cell_passes_on_both(self):
        for fmt in (clarion_format.X08_03, clarion_format.X08_02):
            self.assertTrue(card_check.check_l2_triple_w4(*self.cards(0, 1), fmt)['passed'])

    def test_the_twin_across_the_line_is_qy7s_form_only(self):
        r = card_check.check_l2_triple_w4(*self.cards(0x0040, 3), clarion_format.X08_02)
        self.assertTrue(r['passed'], r)
        self.assertEqual(r['counts']['names the twin across a parcel line'], 1)
        self.assertFalse(card_check.check_l2_triple_w4(*self.cards(0x0040, 3), clarion_format.X08_03)['passed'])

    def test_another_node_of_the_next_parcel_fails(self):
        self.assertFalse(card_check.check_l2_triple_w4(*self.cards(0x0040, 4), clarion_format.X08_02)['passed'])


class TestExpansion(unittest.TestCase):
    def run_rule(self, **statuses):
        walks = [((0, 0), 0, 1, st.replace('_', ' '), 100, 100, [], 7) for st, n in statuses.items() for _ in range(n)]
        old = card_check.expand_l2_links
        card_check.expand_l2_links = lambda L1, L2: iter(walks)
        try: return card_check.check_expansion({}, {})
        finally: card_check.expand_l2_links = old

    def test_the_factory_share_passes_and_one_more_fails(self):
        self.assertTrue(self.run_rule(ok=34785, through_another_L2_node=191)['passed'])
        self.assertFalse(self.run_rule(ok=34784, through_another_L2_node=192)['passed'])

    def test_one_dead_end_fails(self):
        r = self.run_rule(ok=99999, dead_end=1)
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['dead end'], 1)


class TestNodeOrder(unittest.TestCase):
    @staticmethod
    def rec(*keys):
        return dict(nodes=[node(ncls=c, x=x, y=y) for c, x, y in keys])

    def test_class_then_x_then_y_passes(self):
        a = self.rec((3, 9, 9), (7, 1, 5), (7, 1, 6), (7, 2, 0))
        self.assertTrue(card_check.check_node_order({(0, 0): (a, None)}, {}, {(0, 0): a}, {(0, 0): (a, None)})['passed'])

    def test_one_level_1_record_out_of_order_fails(self):
        good, bad = self.rec((7, 1, 5), (7, 2, 0)), self.rec((7, 2, 0), (7, 1, 5))
        L1 = {(0, k): good for k in range(999)}; L1[1, 0] = bad
        r = card_check.check_node_order({}, {}, L1, {})
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['1:ordered'], 999)

    def test_level_2_counts_too(self):
        bad = self.rec((8, 0, 0), (7, 0, 0))
        self.assertFalse(card_check.check_node_order({}, {}, {}, {(0, 0): (bad, None)})['passed'])

    def test_coarse_records_pass_at_the_factory_share(self):
        good, bad = self.rec((7, 1, 5), (7, 2, 0)), self.rec((7, 2, 0), (7, 1, 5))
        lev4 = {(0, k): (good, None) for k in range(509)}
        lev4.update({(1, k): (bad, None) for k in range(4)})
        self.assertTrue(card_check.check_node_order(lev4, {})['passed'])
        lev4[2, 0] = (bad, None)
        self.assertFalse(card_check.check_node_order(lev4, {})['passed'])


class TestL2Nflags(unittest.TestCase):
    def test_fails_when_l2_has_nodes_but_no_flags(self):
        L2 = {(0, 0): (dict(nodes=[node(nflags=0), node(nflags=0)]), None)}
        r = card_check.check_l2_nflags(L2)
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['nodes'], 2)

    def test_passes_when_one_flag_present(self):
        L2 = {(0, 0): (dict(nodes=[node(nflags=0x4000), node(nflags=0)]), None)}
        r = card_check.check_l2_nflags(L2)
        self.assertTrue(r['passed'])

    def test_qy7_has_no_two_level_flag(self):
        qy7 = card_check.clarion_format.get('x08-02')
        L2 = {(0, 0): (dict(nodes=[node(nflags=0x4000), node(nflags=0x2000)]), None)}
        self.assertFalse(card_check.check_l2_nflags(L2, qy7)['passed'])
        L2 = {(0, 0): (dict(nodes=[node(nflags=0x2000), node(nflags=0)]), None)}
        self.assertTrue(card_check.check_l2_nflags(L2, qy7)['passed'])

    def test_passes_on_no_l2_nodes_at_all(self):
        r = card_check.check_l2_nflags({})
        self.assertTrue(r['passed'])


class TestL2EdgeTriples(unittest.TestCase):
    def cards(self, child):
        own = (dict(nodes=[node(x=2047, entries=[entry(cls=0x0404, other=1), entry(cls=0xa404, other=0)])]),
               dict(nodes=[dict(upper=0, parcel=0x00c0, child=5, entries=[(1, 0x00c0, 6), (0, 0x0000, child)])]))
        far = (dict(nodes=[node(x=0, entries=[entry(cls=0x0404, other=1), entry(cls=0xe404, other=0)])]),
               dict(nodes=[dict(upper=0, parcel=0x0000, child=3, entries=[(1, 0x0000, 4), (0, 0x00c0, 5)])]))
        return {(0, 0): own, (0, 1): far}

    def test_the_far_nodes_block_passes(self):
        r = card_check.check_l2_edge_triples(self.cards(3))
        self.assertTrue(r['passed'])
        self.assertEqual(r['counts']['the far block'], 2)

    def test_the_far_level2_index_fails(self):
        r = card_check.check_l2_edge_triples(self.cards(0))
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['not the far block'], 1)

    def test_a_far_parcel_not_loaded_is_counted(self):
        L2 = self.cards(3); del L2[(0, 1)]
        r = card_check.check_l2_edge_triples(L2)
        self.assertTrue(r['passed'])
        self.assertEqual(r['counts']['far parcel off the given tiles'], 1)


class TestL2BlockOrder(unittest.TestCase):
    def blocks(self, *keys):
        return {(0, 0): (dict(nodes=[]), dict(nodes=[dict(upper=i, parcel=p, child=c, entries=[]) for i, (p, c) in enumerate(keys)]))}

    def test_by_cell_then_child_passes(self):
        self.assertTrue(card_check.check_l2_block_order(self.blocks((0, 2), (0, 9), (0x40, 1)))['passed'])

    def test_by_own_node_fails(self):
        r = card_check.check_l2_block_order(self.blocks((0, 9), (0, 2)))
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['out of child order'], 1)


class TestGraphWords(unittest.TestCase):
    def run_rule(self, fmt, **kw):
        L1 = {(0, 0): dict(nodes=[node(entries=[entry(**kw)])])}
        return card_check.check_graph_words(L1, {}, {}, {}, card_check.clarion_format.get(fmt))

    def test_each_format_passes_its_own_form(self):
        self.assertTrue(self.run_rule('x08-03', cls=0x0898, w5=0x8040, extra=[0xf0ff])['passed'])
        self.assertTrue(self.run_rule('x08-02', cls=0x0818, w5=0x8000)['passed'])
        self.assertTrue(self.run_rule('x08-02', cls=0x0a1a, w5=0x0040, extra=[0x13ff])['passed'])

    def test_the_other_formats_form_fails(self):
        self.assertFalse(self.run_rule('x08-02', cls=0x0898)['passed'])
        self.assertFalse(self.run_rule('x08-03', cls=0x0818)['passed'])
        self.assertFalse(self.run_rule('x08-02', cls=0x0818, w5=0x8040, extra=[0xf3ff])['passed'])
        self.assertFalse(self.run_rule('x08-02', cls=0x0818, w5=0x0040, extra=[0x10ff])['passed'])
        self.assertFalse(self.run_rule('x08-03', cls=0x0898, w5=0x8000)['passed'])
        self.assertFalse(self.run_rule('x08-02', cls=0x0818, w5=0x4040, extra=[0x03ff])['passed'])


class TestCoarseGraphPresent(unittest.TestCase):
    def test_single_l2_window_needs_no_graph(self):
        L2 = {(0, 0): (dict(nodes=[]), dict(nodes=[]))}
        r = card_check.check_coarse_graph_present(L2, {}, {})
        self.assertTrue(r['passed'])

    def test_multiple_windows_without_a_graph_fails(self):
        L2 = {(0, 0): (dict(nodes=[]), dict(nodes=[])), (0, 1): (dict(nodes=[]), dict(nodes=[]))}
        r = card_check.check_coarse_graph_present(L2, {}, {})
        self.assertFalse(r['passed'])

    def test_multiple_windows_with_a_graph_passes(self):
        L2 = {(0, 0): (dict(nodes=[]), dict(nodes=[])), (0, 1): (dict(nodes=[]), dict(nodes=[]))}
        lev4 = {(0, 0): (dict(nodes=[]), dict(nodes=[]))}
        lev5 = {(0, 0): (dict(nodes=[]), dict(nodes=[]))}
        r = card_check.check_coarse_graph_present(L2, lev4, lev5)
        self.assertTrue(r['passed'])


class TestCoarseShapes(unittest.TestCase):
    @staticmethod
    def graph(lid, code=0):
        end = lambda at, flag: dict(x=at[0], y=at[1], entries=[dict(cls=0x0494, id=lid | flag, w4=code << 1, w5=0)])
        return {(0, 0): (dict(nodes=[end((10, 20), 0), end((30, 40), 0x2000)]), None)}

    @staticmethod
    def shapes(lid, above=0):
        head = [0, 0x8000 | above, 0] if above else [0, 0]
        shape = [0x8002, 0x4000, 0, 12] + head + [0x0800 | 10, 0x0800 | 20, lid, 0x3000 | 30, 40]
        unit = [0xffff, 0] * 6 + [11, 1] + [0xffff, 0] * 4 + [len(shape) + 1 | (0x2000 if above else 0)] + shape
        return {(0, 0): list(card_check.coarse_shapes(unit))}

    def check(self, above, code=1):
        return card_check.check_coarse_shapes(self.graph(1, code), self.graph(3), {4: self.shapes(1, above), 5: self.shapes(3)})

    def test_shape_with_its_level5_word_passes(self):
        r = self.check(3)
        self.assertTrue(r['passed'], r)
        self.assertEqual(r['counts']['L4 with a route number'], 1)

    def test_missing_level5_word_fails(self):
        r = self.check(0)
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['L4 shapes not naming their level-5 link'], 1)

    def test_word_for_no_level5_link_fails(self):
        self.assertEqual(self.check(4)['counts']['L4 shapes naming no level-5 link'], 1)

    def test_link_level5_lacks_needs_no_word(self):
        self.assertTrue(self.check(0, code=0)['passed'])

    def test_graph_link_without_a_shape_fails(self):
        r = card_check.check_coarse_shapes(self.graph(1), {}, {4: {(0, 1): []}})
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['L4 graph links without a shape'], 1)

    def test_one_way_bits_must_be_the_links(self):
        lev4 = self.graph(1)
        for nd in lev4[0, 0][0]['nodes']: nd['entries'][0]['w5'] = 0x4000
        r = card_check.check_coarse_shapes(lev4, {}, {4: self.shapes(1)})
        self.assertEqual(r['counts']["L4 one-way bits not the link's"], 1)
        self.assertFalse(r['passed'])

    def test_nothing_loaded_passes(self):
        self.assertTrue(card_check.check_coarse_shapes({}, {}, {})['passed'])


class TestCoarseRingCodes(unittest.TestCase):
    def run_rule(self, w4, code):
        lev4 = {(0, 0): (dict(nodes=[node(entries=[entry(cls=0x0404, id=1, w4=w4)]),
                                     node(entries=[entry(cls=0x0404, id=0x2001, w4=w4)])]), None)}
        return card_check.check_coarse_ring_codes(lev4, {}, {'codes': {4: {(0, 0): {(4, 1): code}}}})

    def test_a_ring_link_drawn_as_a_ring_passes(self):
        r = self.run_rule(0x7002, 7)
        self.assertTrue(r['passed'])
        self.assertEqual(r['counts']['L4 ring links'], 1)

    def test_a_ring_link_drawn_as_a_slip_fails(self):
        self.assertFalse(self.run_rule(0x7000, 3)['passed'])

    def test_a_plain_link_drawn_as_a_ring_fails(self):
        self.assertFalse(self.run_rule(0x0000, 7)['passed'])

    def test_a_plain_link_passes(self):
        self.assertTrue(self.run_rule(0x3000, 3)['passed'])


class TestLevel3Direction(unittest.TestCase):
    def run_rule(self, bits3, bits4):
        line = [(25.0, 62.0), (25.001, 62.0), (25.002, 62.0)]
        return card_check.check_level3_direction({'places': {3: [(line, [bits3] * 3)], 4: [((25.001, 62.0001), bits4)]}})

    def test_one_way_at_both_passes(self):
        r = self.run_rule(0x8000, 0x4000)
        self.assertTrue(r['passed'])
        self.assertEqual(r['counts']['one-way at both'], 1)

    def test_two_way_drawn_one_way_fails(self):
        self.assertFalse(self.run_rule(0x4000, 0)['passed'])

    def test_one_way_drawn_two_way_fails(self):
        self.assertFalse(self.run_rule(0, 0x4000)['passed'])

    def test_a_far_shape_is_not_compared(self):
        r = card_check.check_level3_direction({'places': {3: [([(25.0, 62.0), (25.002, 62.0)], [0, 0])], 4: [((25.001, 62.01), 0x4000)]}})
        self.assertTrue(r['passed'])
        self.assertEqual(sum(r['counts'].values()), 0)


class TestRoadContinuity(unittest.TestCase):
    @staticmethod
    def level(roads, unit=1, parcels=((0, 0),)):
        """Roads as (label, points); a unit is this many level-1 units."""
        pc = lambda pts: (pts[0][1] // 2048, pts[0][0] // 2048)
        return dict(scale=(unit, unit), roads=[card_check.Road(label, 4, pts, pc(pts), {4}) for label, pts in roads],
                    parcels=set(parcels), base=(0, 0), long=0)

    def check(self, level4, level1=None, L4=None):
        whole = [('1=VESTURVEGUR;2=54', [(0, 64), (6400, 64)])]
        drawn = {1: self.level(level1 or whole, parcels=[(0, c) for c in range(4)]), 4: self.level(level4, 64)}
        return card_check.check_road_continuity(drawn, {}, {}, L4 or {}, {})

    def test_a_road_drawn_whole_passes(self):
        r = self.check([('2=54', [(0, 1), (40, 1)]), ('2=54', [(40, 1), (100, 1)])])
        self.assertTrue(r['passed'], r)
        self.assertEqual(r['counts']['L4 gaps by name'], 0)

    def test_a_missing_stretch_fails_and_says_where(self):
        r = self.check([('2=54', [(0, 1), (40, 1)]), ('2=54', [(60, 1), (100, 1)])])
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['L4 breaks by name'], 1)
        self.assertIn('level 4: 2=54 is broken between', r['notes'][0])
        self.assertIn('(20 units)', r['notes'][0])

    def test_a_gap_the_width_of_a_crossing_is_not_a_break(self):
        r = self.check([('2=54', [(0, 1), (40, 1)]), ('2=54', [(44, 1), (100, 1)])])
        self.assertTrue(r['passed'], r)
        self.assertEqual((r['counts']['L4 gaps by class'], r['counts']['L4 breaks by class']), (1, 0))

    def test_at_level_1_any_gap_is_a_break(self):
        osm = self.level([('2=54', [(0, 64), (6400, 64)])], parcels=[(0, c) for c in range(4)])
        cut = [('2=54', [(0, 64), (2046, 64)]), ('2=54', [(2049, 64), (6400, 64)])]
        drawn = {1: self.level(cut, parcels=[(0, c) for c in range(4)]), 'osm': osm}
        r = card_check.check_road_continuity(drawn, {}, {}, {}, {})
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['L1 breaks against OSM'], 1)

    def test_a_gap_osm_has_is_not_our_break(self):
        # two OSM ways of one name end two units apart without sharing a node; ours copy them
        apart = [('1=VARAKATU', [(0, 64), (1000, 64)]), ('1=VARAKATU', [(1002, 64), (2000, 64)])]
        osm = self.level(apart, parcels=[(0, c) for c in range(4)])
        drawn = {1: self.level(apart, parcels=[(0, c) for c in range(4)]), 'osm': osm}
        r = card_check.check_road_continuity(drawn, {}, {}, {}, {})
        self.assertTrue(r['passed'], r)
        self.assertEqual(r['counts']['L1 breaks against OSM'], 0)
        # one OSM way, ours cut two units short of where it goes on: still a break
        osm = self.level([('1=VARAKATU', [(0, 64), (1000, 64), (2000, 64)])], parcels=[(0, c) for c in range(4)])
        drawn = {1: self.level(apart, parcels=[(0, c) for c in range(4)]), 'osm': osm}
        self.assertEqual(card_check.check_road_continuity(drawn, {}, {}, {}, {})['counts']['L1 breaks against OSM'], 1)

    def test_a_road_level_1_has_in_two_pieces_may_be_in_two(self):
        apart = [('2=54', [(0, 64), (2560, 64)]), ('2=54', [(3840, 64), (6400, 64)])]
        r = self.check([('2=54', [(0, 1), (40, 1)]), ('2=54', [(60, 1), (100, 1)])], apart)
        self.assertTrue(r['passed'], r)

    def test_the_name_stands_in_where_there_is_no_number(self):
        self.assertEqual(card_check.road_key('1=Vesturvegur;2=54'), '2=54')
        self.assertEqual(card_check.road_key('1=Vesturvegur'), '1=VESTURVEGUR')
        self.assertIsNone(card_check.road_key(''))

    def test_an_end_on_a_parcel_line_needs_a_shape_across(self):
        west, east = ('2=54', [(2000, 1), (2048, 1)]), ('2=54', [(2049, 2), (2100, 2)])
        two = dict(parcels=[(0, 0), (0, 1)])
        ends = lambda roads, dead=(): card_check.open_ends(self.level(roads, **two), dead)
        self.assertEqual(ends([west, east]), (1, []))
        self.assertEqual(ends([west]), (1, [('2=54', (2048, 1))]))
        self.assertEqual(ends([west], {(2048, 1)}), (1, []))
        self.assertEqual(ends([west, ('2=1', [(2048, 1), (2040, 9)])]), (2, []))
        self.assertEqual(card_check.open_ends(self.level([west]))[1], [])    # nothing loaded across

    def test_one_open_end_fails_the_card(self):
        r = self.check([('2=54', [(0, 1), (100, 1)]), ('2=1', [(2000, 5), (2048, 5)])])
        self.assertTrue(r['passed'], r)
        drawn = {1: self.level([('2=54', [(0, 64), (6400, 64)])]),
                 4: self.level([('2=1', [(2000, 5), (2048, 5)])], 64, [(0, 0), (0, 1)])}
        r = card_check.check_road_continuity(drawn, {}, {}, {}, {})
        self.assertFalse(r['passed'])
        self.assertIn('with no shape across', r['notes'][0])

    def test_a_dead_end_is_a_node_with_no_link_across(self):
        node = lambda x, cls: dict(x=x, y=5, entries=[dict(cls=cls)])
        recs = {(0, 0): dict(nodes=[node(2047, 0x0004), node(0, 0x8004)])}
        self.assertEqual(card_check.graph_dead_ends(recs, (1, 2)), {(3 * 2048, 2048 + 5)})

    def test_labels_are_read_by_word_offset(self):
        text = b'2=54'
        sec = b'\0\6\0\1' + b'\xff\xff\0\0' * 5 + b'\0\xff\0\0\0\0\0\0\0\4' + text
        plain = bytearray(44) + sec
        plain[22:26] = (11).to_bytes(2, 'big') + (len(sec) // 4 + 1).to_bytes(2, 'big')
        self.assertEqual(card_check.shape_labels(bytes(plain) + b'\0\0', 'utf-8'), {12: '2=54'})

    def test_nothing_loaded_passes(self):
        self.assertTrue(card_check.check_road_continuity(None, {}, {}, {}, {})['passed'])


class TestNoJunctionOnParcelLine(unittest.TestCase):
    def test_one_real_link_at_an_edge_node_is_fine(self):
        n = node(entries=[entry(cls=0x8000), entry(cls=1)])
        r = card_check.check_no_junction_on_parcel_line({(0, 0): dict(nodes=[n])}, {})
        self.assertTrue(r['passed'])

    def test_two_real_links_at_an_edge_node_fails(self):
        n = node(entries=[entry(cls=0x8000), entry(cls=1), entry(cls=2)])
        r = card_check.check_no_junction_on_parcel_line({(0, 0): dict(nodes=[n])}, {})
        self.assertFalse(r['passed'])


class TestFieldDiff(unittest.TestCase):
    def test_changed_entry_field_reported_with_its_bit(self):
        a = {(0, 0): dict(nodes=[node(entries=[entry(w4=0x0)])])}
        b = {(0, 0): dict(nodes=[node(entries=[entry(w4=0x2)])])}
        rows = card_check.field_diff('T.DAT', a, b, 1)
        by_field = {f: (n, x) for f, n, x in rows}
        self.assertEqual(by_field['T.DAT L1 entry.w4'], (1, 0x2))

    def test_no_change_reports_nothing(self):
        a = {(0, 0): dict(nodes=[node(entries=[entry(w4=1)])])}
        rows = card_check.field_diff('T.DAT', a, a, 1)
        self.assertEqual(rows, [])


class TestBitNote(unittest.TestCase):
    def test_single_bit(self):
        self.assertEqual(card_check.bit_note(0x2), ' (bit 0x2 set)')

    def test_multiple_bits(self):
        self.assertEqual(card_check.bit_note(0x6), ' (bits 0x2/0x4 set)')

    def test_zero_is_silent(self):
        self.assertEqual(card_check.bit_note(0), '')


class TestTiles(unittest.TestCase):
    """The rule that fails a card the ten rules would pass on nothing."""

    def test_empty_card_fails(self):
        self.assertFalse(card_check.check_tiles([], {}, collections.Counter())['passed'])

    def test_expected_cell_missing_fails(self):
        r = card_check.check_tiles(['A', 'B'], {'A': (5, 2)}, collections.Counter())
        self.assertFalse(r['passed'])
        self.assertIn('B: not on the card', r['notes'])

    def test_cell_without_l2_fails(self):
        self.assertFalse(card_check.check_tiles(['A'], {'A': (5, 0)}, collections.Counter())['passed'])

    def test_parse_errors_fail(self):
        r = card_check.check_tiles(['A'], {'A': (5, 2)}, collections.Counter({'A L1': 1}))
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['parse_errors'], 1)

    def test_full_cells_pass(self):
        self.assertTrue(card_check.check_tiles(['A'], {'A': (5, 2), 'B': (1, 1)}, collections.Counter())['passed'])

    def test_unparseable_tile_bytes_count_as_errors(self):
        errors = collections.Counter()
        try:
            card_check.load_road_tiles([b'\0' * 0x2000], errors)
        except Exception:
            return      # a tile too broken to scan raises, which fails the step as well
        self.assertEqual(card_check.check_tiles(['X'], {}, errors)['passed'], False)


class TestUpperLinks(unittest.TestCase):
    def test_walk_takes_the_shape_that_starts_where_the_last_ended(self):
        a, b, c = (0, 0), (5, 0), (9, 0)
        self.assertEqual(card_check.walk_shapes([(b, c), (a, b)], a), 'walked')
        self.assertEqual(card_check.walk_shapes([(a, b), (c, b)], a), 'backwards')
        self.assertEqual(card_check.walk_shapes([(b, a)], a), 'backwards')
        self.assertEqual(card_check.walk_shapes([(a, b), (c, c)], a), 'walk stops short')
        self.assertEqual(card_check.walk_shapes([(b, c)], a), 'no shape at the first node')

    def l2(self, x=100, y=200):
        block = dict(upper=0, parcel=0, child=0, entries=[])
        return {(0, 0): (dict(nodes=[node(x=x, y=y)]), dict(nodes=[block]))}

    def test_a_level_2_node_sits_on_a_flagged_level_1_node(self):
        L1 = {(0, 0): dict(nodes=[node(x=401, y=799, nflags=0x2000)])}
        self.assertTrue(card_check.check_l2_nodes_on_l1(L1, self.l2())['passed'])

    def test_a_level_2_node_inside_a_level_1_link_fails(self):
        L1 = {(0, 0): dict(nodes=[node(x=440, y=800, nflags=0x2000)])}
        r = card_check.check_l2_nodes_on_l1(L1, self.l2())
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['inside a level-1 link'], 1)

    def test_an_unflagged_level_1_node_fails(self):
        L1 = {(0, 0): dict(nodes=[node(x=400, y=800)])}
        r = card_check.check_l2_nodes_on_l1(L1, self.l2())
        self.assertFalse(r['passed'])
        self.assertEqual(r['counts']['level-1 node not flagged'], 1)


class TestTrafficShapes(unittest.TestCase):
    def record(self, flagged):
        import struct
        import fa_encode
        nodes = [(100, 100), (300, 300), (600, 600), (900, 900)]
        chains = [[(1, 0, 1, [])], [(2, 1, 2, [])], [(3, 2, 3, [])]]
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains, hws=['primary'] * 3, country=352)
        buf = bytearray(plain)
        for S in fa_encode.parse_record(plain, 1)[2][:flagged]:
            w1 = struct.unpack_from('>H', buf, 2 * S['at'] + 2)[0]
            struct.pack_into('>H', buf, 2 * S['at'] + 2, w1 | fa_encode.TRAFFIC)
        return {(0, 0): bytes(buf)}

    def test_none_or_most_main_roads_pass(self):
        self.assertTrue(card_check.check_traffic_shapes(self.record(0))['passed'], 'a country with no table')
        self.assertTrue(card_check.check_traffic_shapes(self.record(3))['passed'], 'a country with one')

    def test_a_third_fails(self):
        r = card_check.check_traffic_shapes(self.record(1))
        self.assertFalse(r['passed'])
        self.assertEqual((r['counts']['main set'], r['counts']['main']), (1, 3))


class TestShapeLinkCodes(unittest.TestCase):
    def run_rule(self, level, ring, w4):
        import fa_encode
        plain = fa_encode.build_record(level, 96, 898, 7, 9, [(100, 100), (300, 300)], [[(1, 0, 1, [])]],
                                       hws=['primary'], country=352, rings=[ring])[0]
        graph = dict(nodes=[node(entries=[entry(id=1, cls=0x0494, w4=w4)]), node(entries=[entry(id=0x2001, cls=0x0494, w4=w4)])])
        if level == 1:
            return card_check.check_shape_link_codes({(0, 0): graph}, {}, {(0, 0): plain}, {})
        return card_check.check_shape_link_codes({}, {(0, 0): (graph, None)}, {}, {(0, 0): plain})

    def test_a_ring_shape_over_ring_links_passes(self):
        for level in (1, 2):
            r = self.run_rule(level, True, 0x7002)
            self.assertTrue(r['passed'], r)
            self.assertEqual(r['counts']['%d:shape links' % level], 1)

    def test_a_plain_shape_over_a_ring_link_fails(self):
        for level in (1, 2):
            r = self.run_rule(level, False, 0x7000)
            self.assertFalse(r['passed'])
            self.assertEqual(r['counts']['%d:code differs' % level], 1)

    def test_a_ring_shape_over_a_plain_link_fails(self):
        for level in (1, 2):
            self.assertFalse(self.run_rule(level, True, 0x0002)['passed'])

    def test_a_shape_link_the_graph_lacks_is_counted_not_failed(self):
        import fa_encode
        plain = fa_encode.build_record(1, 96, 898, 7, 9, [(100, 100), (300, 300)], [[(1, 0, 1, [])]], hws=['primary'], country=352)[0]
        r = card_check.check_shape_link_codes({(0, 0): dict(nodes=[])}, {}, {(0, 0): plain}, {})
        self.assertTrue(r['passed'])
        self.assertEqual(r['counts']['1:shape links of no graph link'], 1)


class TestHierarchyCodes(unittest.TestCase):
    def test_the_w4_code_is_the_word_count(self):
        import fa_encode
        nodes = [(100, 100), (300, 300)]
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, nodes, [[(1, 0, 1, [])]], hws=['primary'], country=352,
                                          uppers=[[0x6000, 0x2007]])
        def L1(w4): return {(0, 0): dict(nodes=[node(entries=[entry(id=1, cls=0x0494, w4=w4)])])}
        self.assertTrue(card_check.check_hierarchy_codes(L1(0x4), {(0, 0): plain})['passed'])
        self.assertTrue(card_check.check_hierarchy_codes(L1(0x7004), {(0, 0): plain})['passed'], 'ring bits aside')
        self.assertFalse(card_check.check_hierarchy_codes(L1(0x2), {(0, 0): plain})['passed'])
        many = L1(0x4)
        many[0, 0]['nodes'] += many[0, 0]['nodes'] * 999 + L1(0x2)[0, 0]['nodes']
        self.assertFalse(card_check.check_hierarchy_codes(many, {(0, 0): plain})['passed'], 'one wrong entry is enough')
        qy7 = card_check.clarion_format.get('x08-02')
        self.assertTrue(card_check.check_hierarchy_codes(L1(0x2), {(0, 0): plain}, qy7)['passed'], 'QY7: 1 for any words')
        self.assertFalse(card_check.check_hierarchy_codes(L1(0x4), {(0, 0): plain}, qy7)['passed'])


class TestResultShape(unittest.TestCase):
    def test_counts_and_notes_are_plain_types(self):
        r = card_check.result('x', True, collections.Counter(a=1), ['note'])
        self.assertEqual(r, dict(name='x', passed=True, counts={'a': 1}, notes=['note']))


class TestTwoRecords(unittest.TestCase):
    def test_qy7_graph_records_come_in_two_parts(self):
        import clarion_format
        for level in (2, 4, 5):
            self.assertTrue(card_check.two_records(clarion_format.X08_02, level))
            self.assertFalse(card_check.two_records(clarion_format.X08_03, level))

    def test_a_cell_with_no_roads_fails(self):
        r = card_check.check_tiles([], {'tiles': (0, 0)}, collections.Counter())
        self.assertFalse(r['passed'])


if __name__ == '__main__':
    unittest.main()
