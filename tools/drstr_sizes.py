#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rewrite file sizes inside a DRSTR001.DAT without a card to walk.

The index stores every file's size and each directory's size as the sum of its children,
so a rebuilt tile needs its entry changed and MAPAL001's entry in the root changed to match.
Nothing else moves: entry order and offsets stay as the card had them. Control, 9 Oct 2026:
the service card's index with the four turns-card tile sizes written in is byte for byte the
turns card's own index (md5 fb0e7bcdb2283db18630ba8833c790aa).

    drstr_sizes.py IN.DAT OUT.DAT DIR FILE... [DIR FILE...]     sizes read from the files given
"""
import os, struct, sys


def records(d):
    out, i = [], 0
    while True:
        name, own, parent, cnt = struct.unpack_from('<20sHHH', d, i)
        off = struct.unpack_from('<I', d, i + 0x24)[0]
        out.append((name.rstrip(b'\0').decode(), own, parent, cnt, off)); i += 0x28
        if i >= out[0][4]: return out


def set_sizes(d, dirname, sizes):
    """sizes: {file name: bytes} inside directory dirname; the directory's own entry follows."""
    d = bytearray(d); recs = records(d)
    name, own, parent, cnt, off = [r for r in recs if r[0] == dirname][0]
    total = 0; seen = set()
    for k in range(cnt):
        p = off + 0x20 * k; n = d[p:p + 20].rstrip(b'\0').decode()
        if n in sizes: struct.pack_into('<I', d, p + 0x1c, sizes[n]); seen.add(n)
        total += struct.unpack_from('<I', d, p + 0x1c)[0]
    missing = set(sizes) - seen
    assert not missing, 'not listed under %s: %s' % (dirname, sorted(missing))
    pr = [r for r in recs if r[1] == parent][0]
    for k in range(pr[3]):
        p = pr[4] + 0x20 * k
        if d[p:p + 20].rstrip(b'\0').decode() == dirname: struct.pack_into('<I', d, p + 0x1c, total & 0xffffffff)
    return bytes(d)


if __name__ == '__main__':
    src, dest, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    d = open(src, 'rb').read()
    while args:
        dirname = args.pop(0); files = []
        while args and os.path.isfile(args[0]): files.append(args.pop(0))
        d = set_sizes(d, dirname, {os.path.basename(f): os.path.getsize(f) for f in files})
        for f in files: print('%s/%s %d' % (dirname, os.path.basename(f), os.path.getsize(f)))
    open(dest, 'wb').write(d); print('wrote', dest, len(d), 'bytes')
