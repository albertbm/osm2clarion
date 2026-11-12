#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A card is made under a .partial name: a new run clears a stale one, never a finished card,
and the cards log is made when missing."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import build
import cardcopy
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


class TestChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cards = os.path.join(self.tmp.name, 'cards')
        os.makedirs(os.path.join(cards, 'builds'))
        self.out = os.path.join(cards, 'builds', 'IS-200-ZE1-test.img')
        P = clarion_profile.load('iceland')
        plan, _ = build.generate_steps(P, P.path, '/b.img', self.out, os.path.join(self.tmp.name, 'work'),
                                       None, None, LOCAL, {})
        self.plan = plan
        self.check = dict(plan)['check']
        with open(self.out + '.partial', 'wb') as f:
            f.write(b'card')
        self.sha = cardcopy.sha256(self.out + '.partial')

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, names, sha=None):
        m = dict(post_steps=[dict(name=n, args='', image_sha256=sha or self.sha) for n in names])
        with open(self.out[:-4] + '.manifest.json.partial', 'w') as f:
            json.dump(m, f)

    def test_a_card_without_the_addr_step_fails(self):
        self.manifest(['poi_apply', 'build_poicap-icons'])
        with self.assertRaises(SystemExit) as e:
            self.check()
        self.assertIn('build_poicap-addr never finished', str(e.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_a_card_the_last_step_did_not_write_fails(self):
        self.manifest(list(build.CHAIN), sha='0' * 64)
        with self.assertRaises(SystemExit) as e:
            self.check()
        self.assertIn('not the image build_poicap-addr wrote', str(e.exception))

    def test_me0_without_consent_fails(self):
        P = clarion_profile.load('iceland')
        out = self.out.replace('ZE1', 'ME0')
        plan, _ = build.generate_steps(P, P.path, '/b.img', out, os.path.join(self.tmp.name, 'work'),
                                       None, None, LOCAL, {})
        with self.assertRaises(SystemExit) as e:
            dict(plan)['consent']()
        self.assertIn('consent_cmd', str(e.exception))
        os.replace(self.out + '.partial', out + '.partial')
        with open(out[:-4] + '.manifest.json.partial', 'w') as f:
            json.dump(dict(post_steps=[dict(name=n, image_sha256=self.sha) for n in build.CHAIN]), f)
        with self.assertRaises(SystemExit) as e:
            dict(plan)['check']()
        self.assertIn('needs the consent step', str(e.exception))

    def test_a_unit_with_no_consent_records_none(self):
        self.manifest([])
        dict(self.plan)['consent']()
        with open(self.out[:-4] + '.manifest.json.partial') as f:
            self.assertEqual(json.load(f)['consent'], 'none')

    def test_the_full_chain_passes_this_test(self):
        m = dict(post_steps=[dict(name=n, image_sha256=self.sha) for n in build.CHAIN])
        self.assertEqual(build.chain_problems(m, self.sha, True), [])
        self.assertEqual(build.chain_problems(dict(post_steps=[]), self.sha, False), [])


class TestFinish(TestChain):
    def log(self):
        p = os.path.join(os.path.dirname(os.path.dirname(self.out)), 'CARDS.md')
        with open(p) as f:
            return [l for l in f.read().split('\n') if l.startswith('| 200')]

    def test_a_half_done_rename_is_finished(self):
        with open(self.out[:-4] + '.manifest.json', 'w') as f:
            json.dump({}, f)
        self.check()
        self.assertTrue(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + '.partial'))
        self.assertEqual(len(self.log()), 1)

    def test_a_missing_row_is_added_once(self):
        os.replace(self.out + '.partial', self.out)
        with open(self.out[:-4] + '.manifest.json', 'w') as f:
            json.dump({}, f)
        self.check()
        self.check()
        self.assertEqual(len(self.log()), 1)

    def test_an_existing_card_is_never_replaced(self):
        with open(self.out, 'wb') as f:
            f.write(b'done')
        with self.assertRaises(SystemExit):
            self.check()
        with open(self.out, 'rb') as f:
            self.assertEqual(f.read(), b'done')


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
