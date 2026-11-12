#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Step reuse: a step's tools are the files its scripts import, the stamp holds their contents
and not the repo's HEAD, and the card files a step reads are taken before the stamps are
compared."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlev
import buildstamp

TMP = G = None


def setUpModule():
    global TMP, G
    TMP = tempfile.TemporaryDirectory()
    G = genlev.load(TMP.name)


def tearDownModule():
    TMP.cleanup()


def write(path, text):
    with open(path, 'w') as f:
        f.write(text)


class TestOnlyImportersRerun(unittest.TestCase):
    """Two steps, the second importing c.py: editing a file neither imports reruns nothing,
    editing c.py reruns only the second."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        t = self.d.name
        write(t + '/a.py', 'import b\n')
        write(t + '/b.py', 'X = 1\n')
        write(t + '/c.py', 'import json\nfrom b2 import Y\n')
        write(t + '/b2.py', 'Y = 2\n')
        write(t + '/unrelated.py', 'Z = 3\n')
        self.t = t

    def tearDown(self):
        self.d.cleanup()

    def run_steps(self):
        t = self.t
        steps = []
        for name, script in (('first', 'a.py'), ('second', 'c.py')):
            out = '%s/%s.out' % (t, name)
            steps.append(dict(name=name, inputs=[], args=[script], output_files=[out],
                              tool_groups=[(None, buildstamp.import_closure([t + '/' + script]))]))

        def run_cmds(s):
            write(s['output_files'][0], s['name'])
        buildstamp.run_steps(t, steps, True, run_cmds, on_line=lambda line: None)
        return {s['name']: s['ran'] != 'reused' for s in steps}

    def test_reruns(self):
        self.assertEqual(self.run_steps(), dict(first=True, second=True))
        self.assertEqual(self.run_steps(), dict(first=False, second=False))
        write(self.t + '/unrelated.py', 'Z = 4\n')
        self.assertEqual(self.run_steps(), dict(first=False, second=False))
        write(self.t + '/b2.py', 'Y = 5\n')
        self.assertEqual(self.run_steps(), dict(first=False, second=True))

    def test_closure_follows_imports(self):
        names = [os.path.basename(p) for p in buildstamp.import_closure([self.t + '/c.py'])]
        self.assertEqual(names, ['b2.py', 'c.py'])

    def test_stamp_holds_no_repo_head(self):
        state = buildstamp.input_state(dict(tool_groups=[('/repo', [self.t + '/a.py'])]))
        self.assertEqual(list(state['tools']), [self.t + '/a.py'])


class TestRealSteps(unittest.TestCase):
    """The modules the review found missing from the hand lists are in the closures now."""

    @classmethod
    def setUpClass(cls):
        G.VERSION = '17/09/01/01'
        S = G.osm_steps('/B', '-22,63,-13,67', True) + G.steps(
            '/B', '/B/osm/coast.json', '/base.img', roads=True, area=','.join(G.AREAS), landuse='-22,63,-13,67',
            house=False, coarse_graph=True)
        cls.tools = {s['name']: {os.path.basename(p) for p in G.prep(s)['tools']} for s in S}
        cls.steps = {s['name']: s for s in S}

    def test_missing_modules_are_listed(self):
        self.assertTrue({'clarion_format.py', 'clarion_profile.py'} <= self.tools['tiles'])
        self.assertTrue({'clarion_format.py', 'osm_roads.py'} <= self.tools['rd'])
        self.assertTrue({'sec4_write.py', 'shape_index.py', 'clarion_tile.py'} <= self.tools['lev'])
        self.assertTrue({'iceland_card.py', 'icode_io.py', 'refer_header.py'} <= self.tools['icode'])
        self.assertTrue({'fa_encode.py', 'iceland_card.py', 'poi_point.py'} <= self.tools['addr'])

    def test_an_address_module_reaches_no_map_step(self):
        users = {n for n, t in self.tools.items() if 'zcode_io.py' in t}
        self.assertEqual(users, {'addr'})

    def test_args_carry_the_version_profile_and_config(self):
        a = G.prep(self.steps['addr'])['args']
        self.assertEqual(a['version'], '17/09/01/01')
        self.assertEqual(a['config'], {'addr': G.CONFIG['addr']})
        self.assertIn(G.PROFILE, self.steps['tiles']['cmd'])
        self.assertTrue(os.path.isabs(G.PROFILE))


class TestExtractBeforeHashing(unittest.TestCase):
    def test_another_source_card_reruns_the_step(self):
        with tempfile.TemporaryDirectory() as t:
            B = os.path.join(t, 'B')
            for n, text in (('a', 'one'), ('b', 'two')):
                os.makedirs(os.path.join(t, n))
                write(os.path.join(t, n, 'REFER001_ICODE001.DAT'), text)
            dest = os.path.join(B, 'in', 'REFER001_ICODE001.DAT')
            out = os.path.join(B, 'out.txt')
            ran = []
            for src in ('a', 'a', 'b'):
                s = dict(name='icode', cwd=t, cmd=[sys.executable, '-c', 'open(%r, "w").write("x")' % out],
                         inputs=[dest], extract=[(os.path.join(t, src), 'REFER001/ICODE001.DAT', dest)],
                         products=[out], outputs={})
                G.run_steps([s], B, True)
                ran.append(s['ran'] != 'reused')
            self.assertEqual(ran, [True, False, True])


if __name__ == '__main__':
    unittest.main()
