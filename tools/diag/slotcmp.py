#!/usr/bin/env python3
"""lev5_roads.bg_slot, old body against the new delegation, over every real level-5 record."""
import sys, os, zlib, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import lev_coarse, lev5_roads

def old(w):
    if w[9] not in (0, 0xffff) and w[10]:
        assert w[7] == 0xffff, 'record already has a road block at w7/w8'
        return 9
    assert w[7] != 0xffff and w[8], 'record carries no geometry in either slot'
    assert w[9] in (0, 0xffff) and not w[10], 'w9/w10 = %#x/%#x is not empty' % (w[9], w[10])
    return 7

def call(f, w):
    try: return ('slot', f(w))
    except AssertionError as e: return ('assert', str(e))

n = same = 0
for fn in sys.argv[1:]:
    d = bytearray(open(fn, 'rb').read())
    D = lev_coarse.read_dir(d, 5)
    for s in D['slots']:
        for r in s['recs']:
            if r is None: continue
            w = struct.unpack_from('>22H', zlib.decompress(bytes(r)), 0)
            n += 1
            a, b = call(old, w), call(lev5_roads.bg_slot, w)
            if a == b: same += 1
            else: print('  DIFFERS w7/w8=%04x/%04x w9/w10=%04x/%04x old=%s new=%s' % (w[7], w[8], w[9], w[10], a, b))
print('level-5 records compared: %d, identical result: %d' % (n, same))
