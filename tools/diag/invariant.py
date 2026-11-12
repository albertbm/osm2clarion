#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Over every section-1 record of a tile: does the proposed invariant hold wherever
clarion_bg.parse_record rebuilds byte-exactly, and fail only where it does not?"""
import sys, os, zlib, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import clarion_tile, clarion_bg

for fn in sys.argv[1:]:
    d = open(fn, 'rb').read()
    t = clarion_tile.read_tile(d)
    cls = {}
    for sec_i, sec in enumerate(t['sections']):
        if sec_i: continue
        for L in sec['levels']:
            for D in L['dirs']:
                for row, s in D['slots'].items():
                    for r in s['recs']:
                        if r['raw'][:2] != b'\x58\x85': continue
                        plain = zlib.decompress(r['raw'])
                        w = struct.unpack_from('>22H', plain, 0)
                        if w[2] not in (0x19, 0x3e8, 0xfa, 0xfa0): continue
                        try:
                            rec = clarion_bg.parse_record(plain)
                            exact = clarion_bg.build_record(rec) == plain
                        except Exception as e:
                            cls[('w2=%#x' % w[2], 'raised', type(e).__name__)] = \
                                cls.get(('w2=%#x' % w[2], 'raised', type(e).__name__), 0) + 1
                            continue
                        gi, base = rec['gi'], 4 * w[rec['gi']]
                        table = [struct.unpack_from('>HH', plain, base + 4 * i) for i in range(rec['nlayers'])]
                        end = max([o + l for o, l in table if o != 0xffff] + [rec['nlayers']]) if rec['nlayers'] else 0
                        k = ('w2=%#x' % w[2], 'byte-exact' if exact else 'rebuild differs',
                             'tiles' if end == w[gi + 1] else ('short of declared' if end < w[gi + 1] else 'past declared'))
                        cls[k] = cls.get(k, 0) + 1
    print('%s  level-1/2 tile records' % os.path.basename(fn))
    for k, v in sorted(cls.items(), key=str):
        print('   %6d  %s' % (v, ' | '.join(k)))
