#!/usr/bin/env python3
"""Count the level-5 pointer forms in a LEV3456R.DAT."""
import sys, zlib, struct, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import lev_coarse

def form(v, n):
    if v == 0xffff: return 'ffff/%04x' % n
    if v == 0: return '0000/%04x' % n
    return 'set /%s' % ('set ' if n else '0000')

for fn in sys.argv[1:]:
    d = bytearray(open(fn, 'rb').read())
    D = lev_coarse.read_dir(d, 5)
    forms = {}
    for row, s in enumerate(D['slots']):
        for col, r in enumerate(s['recs']):
            if r is None: continue
            try: p = zlib.decompress(bytes(r))
            except Exception: continue
            w = struct.unpack_from('>22H', p, 0)
            forms.setdefault((hex(w[2]), form(w[7], w[8]), form(w[9], w[10])), []).append((row, col))
    print('%-46s %d bytes, cols=%d rows=%d' % (os.path.basename(fn), len(d), D['cols'], D['rows']))
    for k, v in sorted(forms.items()):
        print('   %4d records  w2=%-6s w7/w8=%-11s w9/w10=%-11s  e.g. r%dc%d' % (len(v), k[0], k[1], k[2], v[0][0], v[0][1]))
