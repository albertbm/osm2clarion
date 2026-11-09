#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""build_gen_lev.py loads a build entirely from --config, with no built-in country name
or personal path - the example config stands in for a real one."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXAMPLE_CONFIG = ROOT / 'examples' / 'config.json'


class TestConfigLoad(unittest.TestCase):
    def test_example_config_is_valid_json(self):
        import json
        json.load(open(EXAMPLE_CONFIG))

    def test_build_gen_lev_loads_example_config(self):
        """--config swaps the whole unit/area/naming table; --help must not need a real card
        or a country name baked into the script."""
        out = subprocess.run([sys.executable, str(ROOT / 'tools' / 'build_gen_lev.py'),
                              '--config', str(EXAMPLE_CONFIG), '--help'],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('--config', out.stdout)

    def test_card_naming_pattern_round_trips(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        env = dict(os.environ, O2C_CONFIG=str(EXAMPLE_CONFIG))
        old = os.environ.get('O2C_CONFIG')
        os.environ['O2C_CONFIG'] = str(EXAMPLE_CONFIG)
        try:
            sys.argv = ['build_gen_lev.py', '--config', str(EXAMPLE_CONFIG)]
            for mod in ('build_gen_lev',):
                sys.modules.pop(mod, None)
            import build_gen_lev as g
            name = g.CARD_BUILD_PATTERN.format(prefix=g.CARD_PREFIX, number=1, unit='EXAMPLE', slug='test')
            self.assertEqual(name, 'EX-001-EXAMPLE-test.img')
            self.assertEqual(g.NAME.match(name).groups(), ('001', 'EXAMPLE', 'test'))
        finally:
            if old is None:
                os.environ.pop('O2C_CONFIG', None)
            else:
                os.environ['O2C_CONFIG'] = old
            sys.modules.pop('build_gen_lev', None)


if __name__ == '__main__':
    unittest.main()
