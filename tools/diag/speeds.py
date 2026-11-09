#!/usr/bin/env python3
"""Implied link speeds in a tile, decoded the way the factory cards decode."""
import os, sys, collections, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan
import clarion_rd

def run(path, lab):
    for lev in (1, 2):
        sp=[]; am=collections.Counter(); bn=collections.Counter(); amax=bmax=0
        with TileScan(path) as t:
            for r in t.records(section=2, level=lev):
                rec=clarion_rd.parse_record(r.plain())
                for nd in rec['nodes']:
                    for e in nd['entries']:
                        if e['cls'] & 0x8000: continue
                        a,b=e['a'],e['b']; am[a>>12]+=1; bn[b>>12]+=1
                        amax=max(amax,a&0xfff); bmax=max(bmax,b&0xfff)
                        m=(a>>12)&7; secs=(a&0xfff)*(4**m)*0.1
                        n=(b>>12)&7; L=(b&0xfff)*(4**n)
                        if secs>0 and L>0: sp.append(L/secs*3.6)
        if not sp: continue
        c=collections.Counter(round(s) for s in sp)
        print('%-22s level %d: %7d links  median %5.1f km/h  modes %s' % (lab,lev,len(sp),statistics.median(sp),c.most_common(6)))
        print('      a>>12 %s  max value %d' % (dict(sorted(am.items())), amax))
        print('      b>>12 %s  max value %d' % (dict(sorted(bn.items())), bmax))

for a in sys.argv[1:]:
    run(a, a.split('/')[-1])
