#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""build.py turns a profile and a local file into the JSON config the generator tools read,
with no personal path in the profile."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import build
import clarion_profile


LOCAL = {'poicap': {'addr_only_default': 'LAUGAVEGUR'}, 'addr': {'poc_streets': {'Laugavegur': 101},
                                                                  'poc_anchor': {'Laugavegur': 1}}}


def config(local=None):
    P = clarion_profile.load('iceland')
    return build.make_config(P, build.merge(LOCAL, local or {}), P.cells, '/cards/bases/base.img', None, '/work')


class TestInterpreters(unittest.TestCase):
    def test_plain_steps_use_this_interpreter(self):
        text = (ROOT / 'tools' / 'build_gen_lev.py').read_text(encoding='utf-8')
        self.assertNotIn("['python3'", text)

    def test_poi_python_is_refused(self):
        with self.assertRaises(SystemExit):
            config({'build': {'poi_python': '/x/python3'}})

    def test_a_library_off_its_pin_is_refused(self):
        from unittest import mock
        with mock.patch.object(build, 'pins', return_value={'shapely': '0.0.1'}):
            with self.assertRaises(SystemExit) as e:
                build.need_deps()
        self.assertIn('shapely', str(e.exception))

    def test_this_environment_has_the_pins(self):
        build.need_deps()


class TestConfig(unittest.TestCase):
    def test_profile_has_no_machine_paths(self):
        for name in ('iceland', 'finland'):
            text = (ROOT / 'profiles' / (name + '.toml')).read_text(encoding='utf-8')
            for bad in ('/Users', '/Volumes', 'albert'):
                self.assertNotIn(bad, text)

    def test_config_is_json_with_the_tools_keys(self):
        cfg = json.loads(json.dumps(config()))
        for key in ('units', 'cells', 'sec4_cells', 'areas', 'road_tiles', 'data', 'card_naming',
                    'cards_log', 'poicap', 'roads_poc', 'addr'):
            self.assertIn(key, cfg)
        self.assertEqual(cfg['cells'], ['B58R078R', 'B58R080R', 'B60R078R', 'B60R080R'])

    def test_the_profile_alone_is_enough(self):
        P = clarion_profile.load('iceland')
        cfg = build.make_config(P, {}, P.cells, '/b.img', None, '/work')
        self.assertNotIn('poc_streets', cfg['addr'])

    def test_a_missing_table_is_named(self):
        P = clarion_profile.load('iceland')
        P.raw = {k: v for k, v in P.raw.items() if k != 'poicap'}
        with self.assertRaises(SystemExit) as e:
            build.make_config(P, {}, P.cells, '/b.img', None, '/work')
        self.assertIn('[poicap]', str(e.exception))

    def test_the_dealer_is_optional(self):
        self.assertIsNone(config()['dealer'])

    def test_icode_defaults_to_the_base_card_and_local_wins(self):
        self.assertEqual(config()['units']['ZE1']['icode'], '/cards/bases/base.img')
        local = {'units': {'ZE1': {'icode': '/x/service.img'}}}
        self.assertEqual(config(local)['units']['ZE1']['icode'], '/x/service.img')

    def test_relative_data_paths_start_at_the_repository_root(self):
        self.assertEqual(config()['data']['pbf'], str(ROOT / 'build' / 'iceland-latest.osm.pbf'))
        local = {'data': {'pbf': '/data/is.pbf'}}
        self.assertEqual(config(local)['data']['pbf'], '/data/is.pbf')

    def test_area_bbox_is_a_list_the_tools_unpack(self):
        for a in config()['areas'].values():
            self.assertEqual(len(a['bbox']), 4)

    def test_build_gen_lev_loads_the_written_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config' / 'iceland.json'
            path.parent.mkdir()
            path.write_text(json.dumps(config()), encoding='utf-8')
            out = subprocess.run([sys.executable, str(ROOT / 'tools' / 'build_gen_lev.py'),
                                  '--config', str(path), '--help'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('--config', out.stdout)

    def test_card_naming_pattern_round_trips(self):
        naming = config()['card_naming']
        name = naming['build_pattern'].format(prefix=naming['prefix'], number=1, unit='ZE1', slug='test')
        self.assertEqual(name, 'IS-001-ZE1-test.img')


class TestCells(unittest.TestCase):
    def test_finland_cells_come_from_the_boxes_and_the_card(self):
        P = clarion_profile.load('finland')
        self.assertEqual(P.cells, [])
        tiles = ['B%02XR%03XR' % (b, r) for b in range(0x28, 0x70, 8) for r in range(0x38, 0x78, 8)]
        want = ['B58R048R', 'B58R050R', 'B58R058R', 'B60R048R', 'B60R050R', 'B68R048R', 'B68R050R']
        self.assertEqual(clarion_profile.cells_for(P.boxes, tiles), want)

    def test_only_cells_the_card_ships_are_used(self):
        P = clarion_profile.load('finland')
        self.assertEqual(clarion_profile.cells_for(P.boxes, ['B58R050R', 'B10R010R']), ['B58R050R'])

    def test_cell_box(self):
        self.assertEqual(clarion_profile.cell_box('B58R050R'), (20, 88 * 2 / 3, 28, 88 * 2 / 3 + 16 / 3))


class TestCameras(unittest.TestCase):
    def test_no_file_means_no_cameras(self):
        import camera_write
        self.assertEqual(camera_write.load_cameras('/no/such/cameras.json', {}), [])

    def test_a_camera_with_no_speed_is_left_out(self):
        import camera_write
        cams = [dict(id='a', speed_limit_kmh=None), dict(id='b', speed_limit_kmh=None), dict(id='c', speed_limit_kmh=50)]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(cams, f)
        try:
            got = camera_write.load_cameras(f.name, {'a': 60})
        finally:
            os.remove(f.name)
        self.assertEqual([(c['id'], c['speed_limit_kmh']) for c in got], [('a', 60), ('c', 50)])


if __name__ == '__main__':
    unittest.main()
