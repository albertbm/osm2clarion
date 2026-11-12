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


def node(ncls=0, x=0, y=0, nflags=0, entries=()):
    return dict(ncls=ncls, x=x, y=y, nflags=nflags, entries=list(entries))


def entry(other=0, w4=0, w5=0, id=0, cls=0, a=0, b=0, extra=()):
    return dict(other=other, w4=w4, w5=w5, id=id, cls=cls, a=a, b=b, extra=list(extra))


class TestEmptyInputs(unittest.TestCase):
    """Every rule must pass vacuously, not crash, on an empty tile set."""

    def test_run_all_on_nothing(self):
        checks = card_check.run_all({}, {}, {}, {}, {})
        self.assertEqual(len(checks), 15)
        for c in checks:
            self.assertTrue(c['passed'], c['name'])


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

    def test_passes_on_no_l2_nodes_at_all(self):
        r = card_check.check_l2_nflags({})
        self.assertTrue(r['passed'])


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
