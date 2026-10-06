#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""DRSTR001.DAT for a card whose MAPAL001 has been replaced.

make_drstr.py builds the index from a directory tree in readdir order, which reproduces
the shipped file. This one differs in what it reads and in what it substitutes, which is
why it is a separate script: every directory keeps the order the card's own
DRSTR001.DAT lists it in, and MAPAL001 is replaced by a given folder of tiles plus the
card's own LEV3456R.DAT. Runs against the mounted card read-only. macOS metadata is skipped.

Reading the order out of the index is the safe way round, not a different theory of the
format. The two orders agree: compared directory by directory against five card images,
the index order is readdir order in every directory of all five. What they disagree about
is VER_INFO in the root, whose entry both European cards rewrote after the index was
mastered, to a different place on each. make_drstr.py has to guess which; this script
reads it.

    ./make_drstr_card.py <mounted card> <folder of new tiles> DRSTR001.DAT
    ./make_drstr_card.py CARD - OUT         # control: must reproduce CARD/DRSTR001.DAT
    --append   keep the card's MAPAL001 order and list new tiles after LEV3456R.DAT
    --ref X    take the order from index file X instead of CARD/DRSTR001.DAT
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_drstr


def main():
    APPEND = '--append' in sys.argv
    REF = sys.argv[sys.argv.index('--ref') + 1] if '--ref' in sys.argv else None
    args = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith('--') and sys.argv[i-1] != '--ref']
    root, tiles, dest = args[:3]
    card_mapal = os.path.realpath(os.path.join(root, 'MAPAL001'))

    def card_order(path):
        """{directory path: [child names in index order]} from the card's own index."""
        d = open(path, 'rb').read()
        recs = []
        i = 0
        while True:
            name, own, parent, cnt = struct.unpack_from('<20sHHH', d, i)
            off = struct.unpack_from('<I', d, i + 0x24)[0]
            recs.append((name.rstrip(b'\0').decode(), own, parent, cnt, off))
            i += 0x28
            if i >= recs[0][4]: break
        paths = {}
        for name, own, parent, cnt, off in recs:
            paths[own] = name if own == parent else os.path.join(paths[parent], name)
        order = {}
        for name, own, parent, cnt, off in recs:
            order[paths[own]] = [d[off + k*0x20:off + k*0x20 + 20].rstrip(b'\0').decode() for k in range(cnt)]
        return order

    ORDER = card_order(REF or os.path.join(root, 'DRSTR001.DAT'))
    real_scandir = os.scandir
    make_drstr.os.scandir = lambda p: [e for e in real_scandir(p) if not e.name.startswith('.') and e.name != 'System Volume Information']
    orig_listing = make_drstr.listing

    def listing(path):
        rel = os.path.relpath(str(path), root)
        key = '/' if rel == '.' else os.path.join('/', rel)
        if tiles != '-' and os.path.realpath(str(path)) == card_mapal:
            new = [(n, False, os.path.getsize(os.path.join(tiles, n))) for n in sorted(os.listdir(tiles)) if n.endswith('.DAT')]
            lev = os.path.join(card_mapal, 'LEV3456R.DAT')
            if os.path.exists(lev): new.append(('LEV3456R.DAT', False, os.path.getsize(lev)))
            if APPEND:   # keep the card's own entries in their own order, add the new names after them
                ref = ORDER.get(key, [])
                keep = [r for r in orig_listing(path) if r[0] in ref]
                keep = sorted(keep, key=lambda r: ref.index(r[0]))
                names = {r[0] for r in keep}
                return keep + [r for r in new if r[0] not in names]
            return sorted(new)
        rows = orig_listing(path)
        ref = ORDER.get(key, [])
        return sorted(rows, key=lambda r: (ref.index(r[0]) if r[0] in ref else len(ref), r[0]))

    make_drstr.listing = listing
    data = make_drstr.build(root)
    open(dest, 'wb').write(data)
    print('wrote', dest, len(data), 'bytes')


if __name__ == '__main__':
    main()
