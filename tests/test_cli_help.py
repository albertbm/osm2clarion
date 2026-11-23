# SPDX-License-Identifier: GPL-3.0-or-later
"""--help and -h on the tools that read sys.argv themselves: the docstring, exit 0, nothing run."""
import os, subprocess, sys, tempfile, unittest, unittest.mock
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / 'tools'
sys.path.insert(0, str(TOOLS))
import cli_help


def scripts():
    return sorted(p for p in list(TOOLS.glob('*.py')) + list(TOOLS.glob('*/*.py'))
                  if p.name != 'cli_help.py' and 'cli_help.help_exit(__doc__)' in p.read_text())


class TestHelp(unittest.TestCase):
    def test_helper(self):
        with unittest.mock.patch.object(sys, 'argv', ['x', 'a.DAT']):
            cli_help.help_exit('doc')
        for flag in ('-h', '--help'):
            with unittest.mock.patch.object(sys, 'argv', ['x', 'a.DAT', flag]), self.assertRaises(SystemExit) as e:
                cli_help.help_exit('doc')
            self.assertEqual(e.exception.code, 0)

    def test_every_tool_prints_its_docstring(self):
        found = scripts()
        self.assertGreater(len(found), 60)
        env = {k: v for k, v in os.environ.items() if k != 'O2C_CONFIG'}
        with tempfile.TemporaryDirectory() as d:
            for p in found:
                for flag in ('--help', '-h'):
                    r = subprocess.run([sys.executable, str(p), flag], cwd=d, env=env, capture_output=True, text=True)
                    self.assertEqual((r.returncode, r.stderr), (0, ''), p.name)
                    self.assertTrue(r.stdout.strip(), p.name)
            self.assertEqual(os.listdir(d), [])


if __name__ == '__main__':
    unittest.main()
