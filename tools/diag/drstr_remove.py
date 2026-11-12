#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove file entries from a directory inside a DRSTR001.DAT. Mirror of drstr_add.py.

Removing a child shrinks its directory's child block by 0x20 bytes per entry, so every
block after it shifts back and the directory's own size in its parent is resummed - same
mechanics as drstr_add.py, run in reverse.

    drstr_remove.py IN.DAT OUT.DAT DIR FILE...
"""
import os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from drstr_add import DIR_REC, CHILD_REC, dir_records, children, child_bytes


def remove(d, dirname, names):
    """names: file names to drop from dirname's child list. Returns the rebuilt index."""
    recs = dir_records(d)
    tgt = [r for r in recs if r['name'] == dirname][0]
    rows = children(d, tgt)
    have = {n for n, _, _ in rows}
    for n in names:
        assert n in have, '%s not listed under %s' % (n, dirname)
    rows = [r for r in rows if r[0] not in names]

    shrink = CHILD_REC * len(names)
    blocks = []
    for r in recs:
        blocks.append(rows if r['name'] == dirname else children(d, r))
    head = bytearray(len(recs) * DIR_REC)
    off = len(head)
    for r, rws in zip(recs, blocks):
        struct.pack_into('<20sHHH', head, r['i'], r['name'].encode('ascii').ljust(20, b'\0'),
                         r['own'], r['parent'], len(rws))
        struct.pack_into('<I', head, r['i'] + 0x24, off)
        off += CHILD_REC * len(rws)
    out = bytearray(head)
    for rws in blocks:
        for n, t, s in rws:
            out += child_bytes(n, t, s)
    # the directory's own size in its parent is the sum of what it lists, truncated
    total = sum(s for _, _, s in rows) & 0xffffffff
    pr = [r for r in recs if r['own'] == tgt['parent']][0]
    po = struct.unpack_from('<I', out, pr['i'] + 0x24)[0]
    for k in range(pr['cnt']):
        p = po + CHILD_REC * k
        if out[p:p + 20].rstrip(b'\0').decode() == dirname:
            struct.pack_into('<I', out, p + 0x1c, total)
    assert len(out) == len(d) - shrink, 'size %d, expected %d' % (len(out), len(d) - shrink)
    return bytes(out), rows, total


def main(argv):
    src, dest, dirname, names = argv[0], argv[1], argv[2], argv[3:]
    d = open(src, 'rb').read()
    out, rows, total = remove(d, dirname, names)
    open(dest, 'wb').write(out)
    print('%s -> %s  %d -> %d bytes' % (os.path.basename(src), os.path.basename(dest), len(d), len(out)))
    for n in names:
        print('  removed %s/%s' % (dirname, n))
    print('  %s now lists %d entries, %d bytes truncated' % (dirname, len(rows), total))


if __name__ == '__main__':
    main(sys.argv[1:])
