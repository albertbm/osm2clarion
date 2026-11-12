#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns, coast, populated cells and cameras are stamped steps written through a temp name, and
the POI step takes a tile from camera_write only when its report says it touched it."""
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
    TestOsmSteps.g = TestCameraStep.g = genlev.load(TMP.name)


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


class TestCameraStep(unittest.TestCase):
    """The cameras step reruns on a changed extract and is reused on the same one."""

    def write_pbf(self, path, lon):
        import osmium
        if os.path.exists(path):
            os.remove(path)
        w = osmium.SimpleWriter(path)
        w.add_node(osmium.osm.mutable.Node(id=1, location=(lon, 64.1), tags={'highway': 'speed_camera',
                                                                            'maxspeed': '50'}))
        w.close()

    def run_step(self, B, box=(-22, 63, -13, 67)):
        s = self.g.camera_step(B, [box])
        self.g.run_steps([s], B, True)
        with open(s['products'][0]) as f:
            import json
            return s['ran'], [round(c['lon'], 4) for c in json.load(f)]

    def test_reruns_on_a_new_extract_only(self):
        try:
            import osmium  # noqa: F401
        except ImportError:
            self.skipTest('needs osmium')
        with tempfile.TemporaryDirectory() as B:
            pbf = os.path.join(B, 'x.osm.pbf')
            old = self.g.PBF
            self.g.PBF = pbf
            try:
                self.write_pbf(pbf, -21.9)
                self.assertEqual(self.run_step(B)[1], [-21.9])
                ran, lons = self.run_step(B)
                self.assertEqual(ran, 'reused')
                self.write_pbf(pbf, -21.8)
                ran, lons = self.run_step(B)
                self.assertNotEqual(ran, 'reused')
                self.assertEqual(lons, [-21.8])
                self.assertNotEqual(self.run_step(B, (-21.85, 63, -13, 67))[0], 'reused', 'a new box reruns')
                self.assertFalse(os.path.exists(self.g.tmp_name(os.path.join(B, 'osm', 'cameras.json'))))
            finally:
                self.g.PBF = old


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
