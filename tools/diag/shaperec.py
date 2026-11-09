#!/usr/bin/env python3
"""Does clarion_bg.parse_record read an fa_encode road-shape record, and does the
'layers tile the geometry section' invariant separate the two record forms?"""
import sys, os, zlib, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import clarion_bg, fa_encode, lev_coarse

NODES = [(100, 200), (900, 240), (1500, 1800)]
chains = fa_encode.chains_from_links([(0, 1, 850), (1, 2, 1600)], {1: 10, 2: 11})

print('=== fa_encode.build_record through clarion_bg.parse_record ===')
for level in (1, 2):
    plain, _ = fa_encode.build_record(level, 96, 898, 7, 9, NODES, chains, hws=['motorway', 'residential'])
    w = struct.unpack_from('>22H', plain, 0)
    print('  level %d: %d bytes, w2=%#06x, w7/w8=%d/%d' % (level, len(plain), w[2], w[7], w[8]))
    try:
        rec = clarion_bg.parse_record(plain)
        back = clarion_bg.build_record(rec)
        kept = sum(len(g.get('raw', b'')) for g in rec['layers'].values())
        print('     parsed: %d layers, raw bytes kept %d of %d in the section, rebuild %s'
              % (len(rec['layers']), kept, 4 * w[8], 'byte-exact' if back == plain else
                 'differs, %d bytes out of %d' % (len(back), len(plain))))
    except Exception as e:
        print('     %s: %s' % (type(e).__name__, e))

def tiles(w, gi, nlayers, table):
    """Do the layer pairs tile the geometry section the header declares?"""
    end = max([off + ln for off, ln in table if off != 0xffff] + [nlayers])
    return end, w[gi + 1]

print()
print('=== the invariant, over every real record ===')
for fn in sys.argv[1:]:
    d = bytearray(open(fn, 'rb').read())
    for level in (5, 4):
        D = lev_coarse.read_dir(d, level)
        cls = {}
        for s in D['slots']:
            for r in s['recs']:
                if r is None: continue
                p = zlib.decompress(bytes(r))
                w = struct.unpack_from('>22H', p, 0)
                try:
                    rec = clarion_bg.parse_record(p)
                    exact = clarion_bg.build_record(rec) == p
                except Exception:
                    cls['raised'] = cls.get('raised', 0) + 1
                    continue
                gi = rec['gi']
                table = []
                base = 4 * w[gi]
                for i in range(rec['nlayers']):
                    table.append(struct.unpack_from('>HH', p, base + 4 * i))
                end, declared = tiles(w, gi, rec['nlayers'], table)
                k = ('byte-exact' if exact else 'rebuild differs',
                     'tiles' if end == declared else 'end %s declared' % ('<' if end < declared else '>'))
                cls[k] = cls.get(k, 0) + 1
        print('  %-22s level %d: %s' % (os.path.basename(os.path.dirname(fn)), level,
              ', '.join('%s %s: %d' % (k[0], k[1], v) if isinstance(k, tuple) else '%s: %d' % (k, v)
                        for k, v in sorted(cls.items(), key=str))))
