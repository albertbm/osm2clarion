#!/usr/bin/env python3
"""Filesystem-level diff of two card images: file list, sizes, mtimes and md5s."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fatread import Fat32

def take(p):
    with Fat32(p) as fs:
        out = {}
        for path, size, fc, mt in fs.walk():
            out[path] = (size, mt, None if size is None else fs.md5(fc, size))
        return out

a, b = take(sys.argv[1]), take(sys.argv[2])
ka, kb = set(a), set(b)
print('entries  A %d   B %d' % (len(a), len(b)))
print('only in A: %s' % sorted(ka - kb))
print('only in B: %s' % sorted(kb - ka))
nsame = 0
for k in sorted(ka & kb):
    if a[k] == b[k]: nsame += 1; continue
    print('DIFFERS %-26s  size %s -> %s   mtime %s -> %s   md5 %s -> %s'
          % (k, a[k][0], b[k][0], a[k][1], b[k][1], a[k][2], b[k][2]))
print('identical in every field: %d of %d' % (nsame, len(ka & kb)))
