#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""--map-from refuses a source stamped for another unit and runs the consent step; the consent
step accepts only success or the consent tool's already-patched exit."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlev
import build
import clarion_profile

TMP = G = None


def setUpModule():
    global TMP, G
    TMP = tempfile.TemporaryDirectory()
    G = genlev.load(TMP.name)


def tearDownModule():
    TMP.cleanup()


class TestVersion(unittest.TestCase):
    def setUp(self):
        self.saved = G.data_version
        G.data_version = lambda files: files['v']

    def tearDown(self):
        G.data_version = self.saved

    def test_another_version_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            G.same_version({'v': '17/09/01/01'}, '14/11/17/01', 'src.img')
        self.assertIn('--restamp-from', str(e.exception))

    def test_the_same_version_passes(self):
        G.same_version({'v': '17/09/01/01'}, '17/09/01/01', 'src.img')


class TestChain(unittest.TestCase):
    def test_map_from_runs_consent(self):
        P = clarion_profile.load('iceland')
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, 'cards', 'builds', 'IS-201-ME0-x.img')
            plan, _ = build.generate_steps(P, P.path, '/b.img', out, os.path.join(t, 'w'), None, None,
                                           genlev.LOCAL, {'map-from': '/src.img'})
        self.assertEqual([n for n, _ in plan], ['prep', 'map', 'consent', 'check'])

    def test_consent_exit_codes(self):
        self.assertTrue(build.consent_ok(0, ''))
        self.assertTrue(build.consent_ok(1, 'card already patched'))
        self.assertFalse(build.consent_ok(1, 'no such file'))
        self.assertFalse(build.consent_ok(2, 'already patched'))


if __name__ == '__main__':
    unittest.main()
