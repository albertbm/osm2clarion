#!/usr/bin/env python3
"""Block-level diff of two raw images, each differing range named against the FAT32 layout."""
import struct, sys

A, B = sys.argv[1], sys.argv[2]
fa, fb = open(A, 'rb'), open(B, 'rb')
mbr = fa.read(512); fa.seek(0)
base = struct.unpack_from('<I', mbr, 446 + 8)[0] * 512
nsec = struct.unpack_from('<I', mbr, 446 + 12)[0]
b = open(A, 'rb'); b.seek(base); bp = b.read(512)
bps = struct.unpack_from('<H', bp, 11)[0]; spc = bp[13]
res = struct.unpack_from('<H', bp, 14)[0]; nfat = bp[16]
spf = struct.unpack_from('<I', bp, 36)[0]; fsinfo = struct.unpack_from('<H', bp, 48)[0]
csz = spc * bps
fat0 = base + res * bps
data = fat0 + nfat * spf * bps
print('p1 %#x .. %#x   fat1 %#x  fat2 %#x  data %#x  cluster %d' %
      (base, base + nsec * 512, fat0, fat0 + spf * bps, data, csz))

def name(off):
    if off < base: return 'before p1'
    if off >= base + nsec * 512: return 'after p1'
    if off < base + bps: return 'p1 boot sector'
    if off < fat0:
        return 'p1 FSInfo' if base + fsinfo * bps <= off < base + (fsinfo + 1) * bps else 'p1 reserved sectors'
    if off < fat0 + spf * bps: return 'FAT copy 1'
    if off < data: return 'FAT copy 2'
    return 'cluster %d' % ((off - data) // csz + 2)

CH = 1 << 22
runs = []
off = 0
while True:
    da, db = fa.read(CH), fb.read(CH)
    if not da and not db: break
    if da != db:
        for i in range(len(da)):
            if da[i] != db[i]:
                o = off + i
                if runs and o == runs[-1][1] + 1: runs[-1][1] = o
                else: runs.append([o, o])
    off += len(da)
print('total bytes compared %d' % off)
# merge runs that fall in the same region
groups = []
for s, e in runs:
    r = name(s)
    if groups and groups[-1][0] == r and s - groups[-1][2] < csz: groups[-1][2] = e; groups[-1][3] += e - s + 1
    else: groups.append([r, s, e, e - s + 1])
print('%d differing byte runs in %d regions:' % (len(runs), len(groups)))
import collections
cl = collections.Counter()
for r, s, e, n in groups:
    if r.startswith('cluster'): cl[int(r.split()[1])] += n
    else: print('  %-20s %#014x .. %#014x  %d bytes' % (r, s, e, n))
if cl:
    ks = sorted(cl)
    out = []
    for c in ks:
        if out and c == out[-1][1] + 1: out[-1][1] = c
        else: out.append([c, c])
    print('  data clusters differing: %d, in runs %s' % (len(ks), ['%d-%d' % (a, b) if a != b else str(a) for a, b in out]))
