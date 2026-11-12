# SPDX-License-Identifier: GPL-3.0-or-later
"""Import build_gen_lev against a config made from the Iceland profile, for tests."""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

LOCAL = {'poicap': {'addr_only_default': 'X'}, 'addr': {'poc_streets': {'X': 101}, 'poc_anchor': {'X': 1}}}


def load(tmp):
    import build
    import clarion_profile
    P = clarion_profile.load('iceland')
    cfg = build.make_config(P, LOCAL, P.cells, '/cards/bases/base.img', None, tmp)
    path = os.path.join(tmp, 'config', 'iceland.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(cfg, f)
    os.environ['O2C_CONFIG'] = path
    if 'build_gen_lev' in sys.modules:
        return importlib.reload(sys.modules['build_gen_lev'])
    return importlib.import_module('build_gen_lev')
