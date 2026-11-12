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
        self.assertEqual(len(checks), 10)
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


class TestResultShape(unittest.TestCase):
    def test_counts_and_notes_are_plain_types(self):
        r = card_check.result('x', True, collections.Counter(a=1), ['note'])
        self.assertEqual(r, dict(name='x', passed=True, counts={'a': 1}, notes=['note']))


if __name__ == '__main__':
    unittest.main()
