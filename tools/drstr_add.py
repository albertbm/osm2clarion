#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add file entries to a directory inside a DRSTR001.DAT, without a card to walk.

`drstr_sizes.py` rewrites the size of an entry that already exists. This adds entries
that do not: a tile for a cell the stock card has no file for. The child block grows by
0x20 bytes per entry, so every block after it shifts and the directory's own size in its
parent is resummed.

**Insert position is sorted, not appended.** Two built cards that a unit has read list
B58R078R.DAT and B58R080R.DAT straight after B58R070R.DAT and B60R078R.DAT and
B60R080R.DAT after B60R060R.DAT, with LEV3456R.DAT still last, while their FAT
directories carry the four appended at the end. So the index order is the sorted one and
need not match the FAT. Control, measured against a second unit's stock index with the
first card's four tile sizes inserted this way: the result comes out at that card's own
length, and differs from it in 25 bytes, every one a size field of an entry this
insertion does not touch - /MAPAL001, /REFER002, MAPAL001/LEV3456R.DAT and 17
REFER002/POINT0xx.DAT. No name, type, child count or child-block offset differs. The
residue is content the two stock databases do not share; byte equality was not available
without the second unit's own stock card.

    drstr_add.py IN.DAT OUT.DAT DIR FILE... [--after NAME]

Files are inserted in sorted order by name among the directory's existing entries, which
is what both cards do. `--after` pins a name that must stay last (LEV3456R.DAT does).
"""
import os, struct, sys

DIR_REC, CHILD_REC = 0x28, 0x20


def dir_records(d):
    out, i = [], 0
    while True:
        name, own, parent, cnt = struct.unpack_from('<20sHHH', d, i)
        off = struct.unpack_from('<I', d, i + 0x24)[0]
        out.append(dict(i=i, name=name.rstrip(b'\0').decode(), own=own, parent=parent,
                        cnt=cnt, off=off))
        i += DIR_REC
        if i >= out[0]['off']:
            return out


def children(d, r):
    out = []
    for k in range(r['cnt']):
        p = r['off'] + CHILD_REC * k
        out.append((d[p:p + 20].rstrip(b'\0').decode(),
                    struct.unpack_from('<H', d, p + 0x16)[0],
                    struct.unpack_from('<I', d, p + 0x1c)[0]))
    return out


def child_bytes(name, typ, size):
    raw = name.encode('ascii')
    assert len(raw) <= 20, 'name too long for the 20-byte field: %s' % name
    return struct.pack('<20sHHII', raw.ljust(20, b'\0'), 0, typ, 0, size & 0xffffffff)


def add(d, dirname, new, last=None):
    """new: [(name, size)]. Returns the rebuilt index."""
    recs = dir_records(d)
    tgt = [r for r in recs if r['name'] == dirname][0]
    rows = children(d, tgt)
    have = {n for n, _, _ in rows}
    for n, _ in new:
        assert n not in have, '%s already listed under %s' % (n, dirname)
    tail = [r for r in rows if last and r[0] == last]
    body = [r for r in rows if not (last and r[0] == last)]
    body += [(n, 0, s) for n, s in new]
    body.sort(key=lambda r: r[0])
    rows = body + tail

    grow = CHILD_REC * len(new)
    out = bytearray(d)
    # the child blocks sit after the record array in record order: rebuild them all
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
    assert len(out) == len(d) + grow, 'size %d, expected %d' % (len(out), len(d) + grow)
    return bytes(out), rows, total


def main(argv):
    last = None
    if '--after' in argv:
        i = argv.index('--after'); last = argv[i + 1]; del argv[i:i + 2]
    src, dest, dirname, files = argv[0], argv[1], argv[2], argv[3:]
    d = open(src, 'rb').read()
    new = [(os.path.basename(f), os.path.getsize(f)) for f in files]
    out, rows, total = add(d, dirname, new, last)
    open(dest, 'wb').write(out)
    print('%s -> %s  %d -> %d bytes' % (os.path.basename(src), os.path.basename(dest), len(d), len(out)))
    for n, s in new:
        print('  added %s/%s %d at position %d' % (dirname, n, s, [r[0] for r in rows].index(n)))
    print('  %s now lists %d entries, %d bytes truncated' % (dirname, len(rows), total))


if __name__ == '__main__':
    main(sys.argv[1:])
