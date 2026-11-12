#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A card is made under a .partial name: a new run clears a stale one, never a finished card,
and the cards log is made when missing."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import build
import cards_log
import clarion_profile

LOCAL = {'poicap': {'addr_only_default': 'X'}, 'addr': {'poc_streets': {'X': 101}, 'poc_anchor': {'X': 1}}}


class TestStale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cards = os.path.join(self.tmp.name, 'cards')
        os.makedirs(os.path.join(cards, 'builds'))
        self.out = os.path.join(cards, 'builds', 'IS-200-ZE1-test.img')
        P = clarion_profile.load('iceland')
        work = os.path.join(self.tmp.name, 'work')
        self.plan, self.before = build.generate_steps(P, P.path, '/b.img', self.out, work, None, None, LOCAL, {})

    def tearDown(self):
        self.tmp.cleanup()

    def names(self):
        return [n for n, _ in self.plan]

    def test_a_stale_partial_is_removed(self):
        open(self.out + '.partial', 'w').close()
        open(self.out[:-4] + '.manifest.json.partial', 'w').close()
        self.before(self.names())
        self.assertFalse(os.path.exists(self.out + '.partial'))
        self.assertFalse(os.path.exists(self.out[:-4] + '.manifest.json.partial'))

    def test_a_finished_card_is_refused(self):
        open(self.out, 'w').close()
        with self.assertRaises(SystemExit):
            self.before(self.names())
        self.assertTrue(os.path.exists(self.out))

    def test_a_later_step_needs_the_card_in_progress(self):
        with self.assertRaises(SystemExit):
            self.before(['check'])
        open(self.out + '.partial', 'w').close()
        self.before(['check'])
        self.assertTrue(os.path.exists(self.out + '.partial'))


class TestCardsLog(unittest.TestCase):
    def test_missing_log_gets_a_header_and_twins_share_a_row(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'CARDS.md')
            cards_log.add(p, 7, 'ZE1', 'x', 'what', os.path.join(d, 'bases', 'b.img'), d)
            cards_log.add(p, 7, 'ZE0', 'x', 'what', os.path.join(d, 'bases', 'b.img'), d)
            with open(p) as f:
                rows = [l for l in f.read().split('\n') if l.startswith('| 007')]
            self.assertEqual(len(rows), 1)
            self.assertIn('| ZE0, ZE1 |', rows[0])
            self.assertIn('Base `bases/b.img`', rows[0])


if __name__ == '__main__':
    unittest.main()
