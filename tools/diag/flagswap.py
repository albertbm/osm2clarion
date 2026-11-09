#!/usr/bin/env python3
"""Swap the two flag resource pointers in substance.skn, by absolute image offset.

Descriptor +24 holds the resource's file offset. Slot 25 is Iceland, slot 26 Italy;
swapping the two pointers leaves both PNGs on the card and touches no pixels.
The file is on the exFAT partition, which no tool here writes, so the write is by
absolute byte offset: the file is contiguous and no length changes.
"""
import sys, struct

A, B = 15326007684, 15326007712        # desc 25 +24, desc 26 +24
ICELAND, ITALY = 20956060, 20956280

img, mode = sys.argv[1], sys.argv[2]
f = open(img, 'r+b' if mode == '--write' else 'rb')
f.seek(A); a = struct.unpack('<I', f.read(4))[0]
f.seek(B); b = struct.unpack('<I', f.read(4))[0]
print('before  %d -> %d   %d -> %d' % (A, a, B, b))
assert (a, b) == (ICELAND, ITALY), 'pointers are not the factory pair; refusing'
if mode != '--write':
    print('dry run, nothing written'); sys.exit()
f.seek(A); f.write(struct.pack('<I', ITALY))
f.seek(B); f.write(struct.pack('<I', ICELAND))
f.flush(); import os; os.fsync(f.fileno())
f.seek(A); a = struct.unpack('<I', f.read(4))[0]
f.seek(B); b = struct.unpack('<I', f.read(4))[0]
print('after   %d -> %d   %d -> %d' % (A, a, B, b))
