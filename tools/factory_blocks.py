#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find 256-byte blocks of a card's map and address files that also occur in a stock card.

Every file in the given folders of each card is cut into 256-byte aligned blocks. Blocks
that are all 00 or all ff are skipped, and so is a short last block. Each remaining block
is looked up among the 256-byte aligned blocks of every file in the same folder of the
stock card. A hit is reported by file and offset with the stock file and offset it
matches; consecutive hits that match consecutive stock blocks are merged into one run.
Constants such as header strings and format tables are expected to hit; they are listed,
not filtered. Both images are opened read-only.

    factory_blocks.py STOCK.img CARD.img [CARD.img ...] [--dirs MAPAL001,REFER001,REFER002]
                      [--json OUT.json]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fatread  # noqa: E402

BLK = 256
DIRS = ('MAPAL001', 'REFER001', 'REFER002')
ZERO, FF = bytes(BLK), b'\xff' * BLK
CHUNK = 64 << 20


def files_in(fs, dirs):
    """(folder, name, size, first cluster) for every file directly inside the folders."""
    for p, sz, c, _ in fs.walk():
        parts = p.strip('/').split('/')
        if sz is not None and len(parts) == 2 and parts[0] in dirs:
            yield parts[0], parts[1], sz, c


def stream(fs, clus, size):
    """A file's bytes in chunks of about CHUNK, reading contiguous cluster runs at once."""
    f = fs.f
    csz = f.spc * f.bps
    chain = f.chain(clus) if size else []
    left, i = size, 0
    while i < len(chain) and left > 0:
        j = i + 1
        while j < len(chain) and chain[j] == chain[j - 1] + 1 and (j - i) * csz < CHUNK:
            j += 1
        d = f.rd(f.csec(chain[i]) * f.bps, (j - i) * csz)[:left]
        left -= len(d)
        yield d
        i = j


def card_blocks(img, dirs):
    """{folder: {block: [(name, offset), ...]}} and per-file counts."""
    want, stats = {}, {}
    with fatread.Fat32(img) as fs:
        for d, n, sz, c in files_in(fs, dirs):
            st = stats[d + '/' + n] = dict(size=sz, blocks=0, skipped=0, hits=0)
            off = 0
            tail = b''
            for chunk in stream(fs, c, sz):
                buf = tail + chunk
                k = len(buf) // BLK * BLK
                for i in range(0, k, BLK):
                    b = buf[i:i + BLK]
                    st['blocks'] += 1
                    if b == ZERO or b == FF:
                        st['skipped'] += 1
                    else:
                        want.setdefault(d, {}).setdefault(b, []).append((n, off + i))
                off += k
                tail = buf[k:]
            if tail:
                st['short_tail'] = len(tail)
    return want, stats


def scan_stock(img, dirs, want):
    """{folder: {block: (stock name, offset, occurrences)}} for every wanted block found."""
    found = {d: {} for d in want}
    with fatread.Fat32(img) as fs:
        for d, n, sz, c in files_in(fs, dirs):
            w = want.get(d)
            if not w:
                continue
            f = found[d]
            off = 0
            tail = b''
            for chunk in stream(fs, c, sz):
                buf = tail + chunk
                k = len(buf) // BLK * BLK
                for i in range(0, k, BLK):
                    b = buf[i:i + BLK]
                    if b in w:
                        if b in f:
                            f[b] = (f[b][0], f[b][1], f[b][2] + 1)
                        else:
                            f[b] = (n, off + i, 1)
                off += k
                tail = buf[k:]
    return found


def runs(hits):
    """Merge (offset, stock name, stock offset, count, block) hits into runs."""
    out = []
    for off, sn, so, cnt, b in sorted(hits):
        r = out[-1] if out else None
        if r and off == r['off'] + BLK * r['blocks'] and sn == r['stock'] and so == r['stock_off'] + BLK * r['blocks']:
            r['blocks'] += 1
        else:
            out.append(dict(off=off, blocks=1, stock=sn, stock_off=so, stock_copies=cnt,
                            head=b[:32].hex(), text=''.join(chr(x) if 32 <= x < 127 else '.' for x in b[:64])))
    return out


def main(argv):
    dirs = DIRS
    if '--dirs' in argv:
        i = argv.index('--dirs')
        dirs = tuple(argv[i + 1].split(','))
        del argv[i:i + 2]
    out_json = None
    if '--json' in argv:
        i = argv.index('--json')
        out_json = argv[i + 1]
        del argv[i:i + 2]
    stock, cards = argv[0], argv[1:]
    t0 = time.time()
    per = {}
    want = {}
    for img in cards:
        w, stats = card_blocks(img, dirs)
        per[img] = (w, stats)
        for d, blocks in w.items():
            want.setdefault(d, {}).update(dict.fromkeys(blocks))
        print('%s: %d files, %d blocks to look up (%.0f s)' % (
            os.path.basename(img), len(stats), sum(len(b) for b in w.values()), time.time() - t0))
    found = scan_stock(stock, dirs, want)
    print('stock scanned (%.0f s)' % (time.time() - t0))
    result = dict(stock=stock, dirs=list(dirs), cards={})
    for img in cards:
        w, stats = per[img]
        hits = {}
        for d, blocks in w.items():
            for b, places in blocks.items():
                if b in found.get(d, {}):
                    sn, so, cnt = found[d][b]
                    for n, off in places:
                        hits.setdefault(d + '/' + n, []).append((off, sn, so, cnt, b))
        files = {}
        for path, st in sorted(stats.items()):
            r = runs(hits.get(path, []))
            st['hits'] = sum(x['blocks'] for x in r)
            files[path] = dict(st, runs=r)
        result['cards'][img] = files
        print()
        print('== %s against %s' % (os.path.basename(img), os.path.basename(stock)))
        print('%-28s %12s %9s %9s %7s' % ('file', 'size', 'blocks', 'skipped', 'hits'))
        for path, f in files.items():
            print('%-28s %12d %9d %9d %7d' % (path, f['size'], f['blocks'], f['skipped'], f['hits']))
        for path, f in files.items():
            for x in f['runs']:
                print('  %s +%#x %d block%s = %s +%#x (%d cop%s)  %s' % (
                    path, x['off'], x['blocks'], 's' if x['blocks'] > 1 else '', x['stock'], x['stock_off'],
                    x['stock_copies'], 'ies' if x['stock_copies'] > 1 else 'y', x['text'][:48]))
    if out_json:
        with open(out_json, 'w') as fh:
            json.dump(result, fh, indent=1)


if __name__ == '__main__':
    main(sys.argv[1:])
