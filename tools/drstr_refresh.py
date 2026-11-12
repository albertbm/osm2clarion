#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Refresh DRSTR001.DAT size fields after a file on the card changed size.

The unit reads each file's size from this index, not from the FAT, so any step that writes
a file of another size onto the card calls this afterwards.

For a plain file actually present on the card under DIR/NAME, the real FAT size is used.
For a packed entry (POINT001.DAT and friends, stored on the card as NAME.cmp + NAME.ind),
DRSTR holds the plain size, which nothing on the card can supply, so give it explicitly.

    drstr_refresh.py IMAGE DIR:NAME[=SIZE] [DIR:NAME[=SIZE] ...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fatedit import Editor  # noqa: E402
from drstr_sizes import set_sizes  # noqa: E402


def refresh(img, entries):
    """entries: {(dir, name): size or None}; None reads the real FAT size."""
    ed = Editor(img)
    fs = ed.fs
    sizes = {}
    for (d, n), sz in entries.items():
        if sz is None:
            g = ed.find(d, n)
            assert g is not None, '%s/%s not on the card' % (d, n)
            sz = g.size
        sizes.setdefault(d, {})[n] = sz
    drs = ed.find('', 'DRSTR001.DAT')
    drs_data = b''.join(fs.rd(fs.coff(c), fs.csz) for c in fs.chain(drs.first))[:drs.size]
    for d, sz in sizes.items():
        drs_data = set_sizes(drs_data, d, sz)
    ed.write('', 'DRSTR001.DAT', drs_data)
    free = ed.close()
    return sizes, free


def main(argv):
    img = argv[0]
    entries = {}
    for a in argv[1:]:
        spec, _, sz = a.partition('=')
        d, n = spec.split(':', 1)
        entries[(d, n)] = int(sz) if sz else None
    sizes, free = refresh(img, entries)
    for d, m in sizes.items():
        for n, sz in m.items():
            print('refreshed', d, n, sz)
    print('free after', free)


if __name__ == '__main__':
    main(sys.argv[1:])
