#!/usr/bin/env python3
"""Copy one file out of a card image by walking the FAT. Caches FAT sectors so the
cluster chain costs a handful of reads instead of one per cluster."""
import sys, struct, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fatread import Fat

img, path, dest = sys.argv[1], sys.argv[2], sys.argv[3]
f = Fat(img); f.open_part(f.parts[0][2])
clus, size = None, None
for comp in path.split('/'):
    ents = f.readdir(clus)
    m = [e for e in ents if e[0].upper() == comp.upper()]
    if not m:
        sys.exit('not found: %s' % comp)
    clus, size = m[0][2], m[0][3]

CH = 1 << 20
cache = {}
def fat_entry(c):
    off = f.fatstart * f.bps + c * 4
    base = off & ~(CH - 1)
    if base not in cache:
        cache[base] = f.rd(base, CH)
    return struct.unpack_from('<I', cache[base], off - base)[0] & 0x0fffffff

n = 0
with open(dest, 'wb') as out:
    c = clus
    while c and c < 0x0ffffff8 and n < size:
        run = 1                                  # merge contiguous clusters into one read
        while n + run * f.spc * f.bps < size and fat_entry(c + run - 1) == c + run:
            run += 1
            if run > 64: break
        d = f.rd(f.csec(c) * f.bps, run * f.spc * f.bps)
        d = d[:size - n]; out.write(d); n += len(d)
        c = fat_entry(c + run - 1)
print('wrote %s, %d of %d bytes' % (dest, n, size))
