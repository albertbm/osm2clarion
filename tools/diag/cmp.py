#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare two tiles record by record, keyed on (section, level, slot, k)."""
import os, sys, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan

a, b = sys.argv[1], sys.argv[2]
def grab(p):
    with TileScan(p) as t:
        secs = dict(t.sections())
        d = {}
        for r in t.records():
            d.setdefault((r.section, r.level, r.dir_off, r.slot, r.k), []).append(r.raw())
        return secs, d
# dir_off differs between files, so key on (section, level, order of dir) instead
def grab2(p):
    with TileScan(p) as t:
        secs = dict(t.sections())
        dirs = {}
        d = {}
        for r in t.records():
            key = (r.section, r.level)
            dl = dirs.setdefault(key, [])
            if r.dir_off not in dl: dl.append(r.dir_off)
            d[(r.section, r.level, dl.index(r.dir_off), r.slot, r.k)] = r.raw()
        hdr = bytes(t.m[0:0x1000])
        return secs, d, hdr

sa, da, ha = grab2(a); sb, db, hb = grab2(b)
print('  level table  old %s' % sa)
print('               new %s' % sb)
ka, kb = set(da), set(db)
print('  records old %d  new %d   only-old %d  only-new %d' % (len(da), len(db), len(ka-kb), len(kb-ka)))
import collections
print('  only-new by (sec,lev): %s' % dict(collections.Counter((k[0], k[1]) for k in kb-ka)))
print('  only-old by (sec,lev): %s' % dict(collections.Counter((k[0], k[1]) for k in ka-kb)))
diff = [k for k in ka & kb if da[k] != db[k]]
print('  shared records differing: %d  %s' % (len(diff), dict(collections.Counter((k[0], k[1]) for k in diff))))
# header diff
hd = [i for i in range(0x1000) if ha[i] != hb[i]]
print('  header bytes 0..0x1000 differing: %d at %s' % (len(hd), ['%#x' % i for i in hd[:40]]))
