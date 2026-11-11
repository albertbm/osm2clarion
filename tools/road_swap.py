#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Swap a factory tile's roads for ours, keeping the tile's own structure.

Header, section tables, every directory, section 4 and the background records stay as the
factory wrote them. Only slots change: in section 1 the road pairs and in section 2 the graph
records are emptied, or filled from a tile rd_build.py wrote (ROADS) for the parcels it
covers; there the background record comes from ROADS too, since its names section has to
match the new road shapes. A row block that shrinks is rewritten where it was and one that
grows goes to the end of the file, so only slot addresses and length words move. An extended
slot stays extended.

A QY7 unit drew our road records inside a factory tile written this way. Section 4 keeps its
signposts here, which now point at roads that are gone; road_strip.py cuts them to the country.

LEV3456R.DAT gets the same treatment with an empty road set: in section 1 the road pair of
levels 4 and 3 is emptied and a level-5 record loses its road block (road_strip.cut_road_block),
section 2's coarse graph rows are emptied, and every block is rewritten where it was.

    road_swap.py --profile NAME SRC OUT [ROADS]
"""
import collections
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_profile
import lev_file
import road_strip
import tile_header
from clarion_tile import enc_addr, pad256, walk
from tile_scan import TileScan, addr_of

ROAD_ABSENT = {1: (0x7fffffff, 0x7fffffff), 2: (0xffffffff, 0xffffffff)}
ABSENT = (0xffffffff, 0xffffffff)


def by_slot(t, sections=(1, 2)):
    out = collections.defaultdict(list)
    for r in t.records():
        if r.section in sections and r.level in (1, 2):
            out[(r.dir_off, r.slot)].append(r)
    return out


def ours_index(path, fmt):
    """{(section, level, y, x, row): {k: (raw, words)}} from a tile in plain (unextended) layout."""
    out = collections.defaultdict(dict)
    with TileScan(path) as t:
        assert t.fmt is fmt
        for r in t.records():
            if r.section not in (1, 2) or r.level not in (1, 2):
                continue
            assert not r.ext and not r.sub
            _, _, _, y, x, _ = struct.unpack_from('>6H', t.m, r.dir_off)
            out[(r.section, r.level, y, x, r.slot)][r.k] = (r.raw(), r.words)
    return out


def pair_of(raw, words, zlib_rec):
    return (len(raw) // 2 if zlib_rec else len(raw) // 2 - words, words)


def swap(src, out, fmt, roads=None):
    d = bytearray(open(src, 'rb').read())
    ours = ours_index(roads, fmt) if roads else {}
    used, stats = set(), collections.Counter()
    with TileScan(src) as t:
        if t.fmt is not fmt:
            raise SystemExit('%s is %s, the profile says %s' % (src, t.fmt.name, fmt.name))
        recs = by_slot(t)
        for sid, lev, doff, slot, p, tag, ext, b in t.slots():
            if sid not in (1, 2) or lev not in (1, 2):
                continue
            _, _, _, y, x, _ = struct.unpack_from('>6H', t.m, doff)
            mine = ours.get((sid, lev, y, x, slot), {})
            if mine:
                used.add((sid, lev, y, x, slot))
            if tag == 0xffffffff:
                if mine:
                    stats['our rows with no factory row, skipped'] += 1
                continue
            old = recs[(doff, slot)]
            npairs = b // 8
            pairs = [ABSENT] * npairs
            block = []
            if sid == 1:
                per = 4 if ext else 2
                stock_bg = {r.col: r for r in old if not r.is_road_pair}
                for col in range(npairs // per):
                    rk, bk = per * col, per * col + per - 1
                    if 2 * col in mine and 2 * col + 1 in mine:
                        for k, (raw, words) in ((rk, mine[2 * col]), (bk, mine[2 * col + 1])):
                            pairs[k] = pair_of(raw, words, fmt.is_zlib(raw)); block.append(raw)
                        stats['parcels with our roads'] += 1
                        continue
                    pairs[rk] = ROAD_ABSENT[lev]
                    r = stock_bg.get(col)
                    if r is not None:
                        raw = r.raw()
                        pairs[bk] = pair_of(raw, r.words, r.is_zlib); block.append(raw)
                    elif 2 * col in mine:
                        stats['our parcels with no factory background, skipped'] += 1
                if ext:
                    ln, x0 = pairs[0]
                    pairs[0] = (ln | 0x80000000, x0) if ln not in (0xffffffff, 0x7fffffff) else (0xffffffff, 0x7fffffff)
            else:
                for k, (raw, words) in sorted(mine.items()):
                    pairs[k] = pair_of(raw, words, fmt.is_zlib(raw)); block.append(raw)
            data = b''.join(block)
            body = b''.join(struct.pack('>II', *pr) for pr in pairs)
            if not data:
                tag_new = 0xffffffff
            else:
                at = addr_of(tag)
                span = sum(r.n for r in old)
                if len(data) <= span:
                    d[at:at + span] = data + bytes(span - len(data))
                    tag_new = tag
                else:
                    at = pad256(len(d))
                    d += bytes(at - len(d)) + data
                    d += bytes(pad256(len(d)) - len(d))
                    tag_new = enc_addr(at)
                    stats['row blocks moved to the end'] += 1
            struct.pack_into('>I', d, p, tag_new)
            d[p + 4:p + 4 + b] = body
    missing = set(ours) - used
    stats['our rows with no factory directory, skipped'] = len(missing)
    with open(out, 'wb') as f:
        f.write(d)
    return stats, len(d)


def swap_lev(src, out, fmt):
    """The coarse file with its roads removed in place; no road set goes in."""
    d = bytearray(open(src, 'rb').read())
    if clarion_format_of(d) is not fmt:
        raise SystemExit('%s is not %s' % (src, fmt.name))
    stats = collections.Counter()
    levels, _ = walk(d, tile_header.LEVEL_OFF, 4)
    _, sect = tile_header.fields(bytes(d[:0x1000]))
    for (sid, lv), (ta, _) in zip(levels, sect):
        ta = addr_of(ta)
        w = struct.unpack_from('>%dH' % struct.unpack_from('>H', d, ta)[0], d, ta)
        for li, e in enumerate(lv):
            o = w[1 + li]
            if o == 0xffff:
                continue
            for g in range(w[o + 1]):
                hi, lo = w[o + 6 + 6 * g], w[o + 7 + 6 * g]
                lev_dir(d, addr_of(hi << 16 | lo), sid, e[0], fmt, stats)
    with open(out, 'wb') as f:
        f.write(d)
    return stats, len(d)


def clarion_format_of(d):
    import clarion_format
    return clarion_format.of_header(d[:0x1000])


def lev_dir(d, da, sid, level, fmt, stats):
    size, nrows, cols, y, x, typ = struct.unpack_from('>6H', d, da)
    pairs = 1 if nrows == cols == 1 else ((2 * size - 12) // nrows - 4) // 8
    assert lev_file.head_size(nrows, cols, pairs) == size
    q = da + 12
    for _ in range(nrows):
        tag = struct.unpack_from('>I', d, q)[0]
        if tag != 0xffffffff:
            pr = [struct.unpack_from('>II', d, q + 4 + 8 * k) for k in range(pairs)]
            at = b = addr_of(tag)
            old, new, newpr = 0, [], []
            for k, (ln, X) in enumerate(pr):
                if ln == 0xffffffff:
                    newpr.append((ln, X)); continue
                z = fmt.is_zlib(d[b:b + 2])
                n = 2 * ln if z else 2 * (ln + X)
                raw = bytes(d[b:b + n]); b += n; old += n
                road = sid == 2 or (pairs == 2 * cols and k % 2 == 0)
                if road:
                    newpr.append((0xffffffff, 0xffffffff)); stats['records dropped'] += 1; continue
                if level == 5 and z:
                    plain = road_strip.cut_road_block(fmt.inflate(raw))
                    if len(plain) != 2 * X:
                        raw = fmt.deflate(plain); ln, X = len(raw) // 2, len(plain) // 2
                        stats['level-5 road blocks cut'] += 1
                new.append(raw); newpr.append((ln, X)); stats['records kept'] += 1
            data = b''.join(new)
            assert len(data) <= old
            d[at:at + old] = data + bytes(old - len(data))
            if not data:
                struct.pack_into('>I', d, q, 0xffffffff)
                newpr = [(0xffffffff, 0xffffffff)] * pairs
            for k, pr_ in enumerate(newpr):
                struct.pack_into('>II', d, q + 4 + 8 * k, *pr_)
        q += 4 + 8 * pairs


if __name__ == '__main__':
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    if os.path.basename(argv[0]).upper().endswith('LEV3456R.DAT'):
        assert len(argv) == 2, 'the coarse file takes no road set yet'
        stats, size = swap_lev(argv[0], argv[1], P.fmt)
    else:
        stats, size = swap(argv[0], argv[1], P.fmt, argv[2] if len(argv) > 2 else None)
    print('%s -> %s, %d bytes' % (argv[0], argv[1], size))
    for k, v in sorted(stats.items()):
        print('  %-48s %d' % (k, v))
