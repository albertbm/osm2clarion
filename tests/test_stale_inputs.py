#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns, coast and populated cells are stamped steps written through a temp name, and the POI
step takes a tile from camera_write only when its report says it touched it."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlev

TMP = None


def setUpModule():
    global TMP
    TMP = tempfile.TemporaryDirectory()
    TestOsmSteps.g = genlev.load(TMP.name)


def tearDownModule():
    TMP.cleanup()


class TestOsmSteps(unittest.TestCase):
    def steps(self, box='-22,63,-13,67'):
        return {s['name']: s for s in self.g.osm_steps('/B', box, True)}

    def test_each_reads_the_extract_and_lands_through_a_temp_name(self):
        for s in self.steps().values():
            self.assertIn(self.g.PBF, s['inputs'])
            for p in s['products']:
                moves = [c for c in s['cmds'] if c[-1] == p and 'os.replace' in c[2]]
                self.assertEqual(len(moves), 1, p)
                self.assertTrue(any(moves[0][-2] in ' '.join(c) for c in s['cmds'][:-1]))

    def test_another_box_changes_the_populated_step(self):
        a, b = self.steps()['populated'], self.steps('-22,63,-13,66')['populated']
        self.assertNotEqual(self.g.prep(dict(a))['args'], self.g.prep(dict(b))['args'])

    def test_house_reads_the_populated_cells(self):
        s = self.steps()
        self.assertEqual(sorted(s), ['coast', 'populated', 'turns'])
        self.assertIn('/B/osm/house_cells.json', s['populated']['products'])


class TestCameraSources(unittest.TestCase):
    def test_a_stale_camera_tile_is_not_taken(self):
        with tempfile.TemporaryDirectory() as d:
            cam_in, cam_out = os.path.join(d, 'in'), os.path.join(d, 'out')
            os.makedirs(cam_out)
            open(os.path.join(cam_out, 'T1.DAT'), 'w').close()    # from an earlier build
            report = {'cam': [dict(tile='T2.DAT', level=1)]}
            import camera_write
            src = camera_write.sources(['T1', 'T2'], report, cam_in, cam_out)
            self.assertEqual(src['T1'], os.path.join(cam_in, 'T1.DAT'))
            self.assertEqual(src['T2'], os.path.join(cam_out, 'T2.DAT'))


if __name__ == '__main__':
    unittest.main()
