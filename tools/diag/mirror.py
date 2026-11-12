#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sparse mirror of a card image's map partition: same tree, same names, same file sizes.

make_drstr.py reads only the directory structure, the file sizes and the first four
bytes of each .ind file, so a tree of sparse files reproduces its output exactly
without mounting the image or spending the 15 GB.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fatread import Fat32

img, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
nd = nf = 0
with Fat32(img) as fs:
    for p, size, fc, mt in fs.walk():
        t = out + p
        if size is None:
            os.makedirs(t, exist_ok=True); nd += 1; continue
        nf += 1
        os.makedirs(os.path.dirname(t), exist_ok=True)
        if p.endswith('.ind'):                      # make_drstr reads its first 4 bytes
            head = next(fs.stream(fc, min(size, 4096)), b'') if size else b''
            with open(t, 'wb') as f: f.write(head); f.truncate(size)
        else:
            with open(t, 'wb') as f: f.truncate(size)   # sparse
print('%d dirs, %d files' % (nd, nf))
