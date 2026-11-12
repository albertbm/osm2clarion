#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse-then-build every section-1 level 5 and 4 record of a LEV3456R.DAT and
count the byte-exact rebuilds, against a chosen clarion_bg module.

    ./roundtrip.py <old|new> FILE...
"""
import sys, os, zlib, struct, importlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import lev_coarse

which = sys.argv[1]
cb = importlib.import_module('clarion_bg_old' if which == 'old' else 'clarion_bg')
print('clarion_bg: %s' % cb.__file__)

TOT = {}
for fn in sys.argv[2:]:
    d = bytearray(open(fn, 'rb').read())
    for level in (5, 4):
        D = lev_coarse.read_dir(d, level)
        ok = bad = err = 0
        misplaced = []
        for row, s in enumerate(D['slots']):
            for col, r in enumerate(s['recs']):
                if r is None: continue
                p = zlib.decompress(bytes(r))
                w = struct.unpack_from('>22H', p, 0)
                mis = w[7] != 0xffff and w[8] and w[9] == 0 and w[10] == 0
                try:
                    rec = cb.parse_record(p)
                    b = cb.build_record(rec)
                    if b == p: ok += 1
                    else: bad += 1
                    if mis: misplaced.append((row, col, 'gi=%d' % rec['gi'], 'exact' if b == p else 'rebuilt differs'))
                except Exception as e:
                    err += 1
                    if mis: misplaced.append((row, col, type(e).__name__, repr(e)[:70]))
        print('  %-28s level %d: %4d byte-exact, %4d mismatched, %3d errors'
              % (os.path.basename(os.path.dirname(fn)) + '/' + os.path.basename(fn), level, ok, bad, err))
        k = TOT.setdefault(level, [0, 0, 0])
        k[0] += ok; k[1] += bad; k[2] += err
        for m in misplaced:
            print('        w9/w10=0000/0000 record r%dc%d: %s %s' % m)
for level, (a, b, c) in sorted(TOT.items()):
    print('  TOTAL level %d: %d byte-exact, %d mismatched, %d errors' % (level, a, b, c))
