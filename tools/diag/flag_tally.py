#!/usr/bin/env python3
"""Tally the level-1 x-flag 0x0800 bit by point role, the test for the multipoint fix.

Replaces the lost `bitstudy.py` whose output REPORT-rebuild-fixed.md section 2 records:
on fixed tiles every start, first-of-link and inner point has 0x0800 set and every link
end and last point has it clear, with no exceptions. A tile built before 13 Oct 2026 has
the bit clear on first-of-link and inner points instead.

Roles follow fa_encode.shape_words: start (the shape's own first point), first-of-link
(the first point after a link header), inner, linkend (a link's last point inside the
chain) and last (the shape's final point).

    flag_tally.py TILE...
"""
import collections, struct, sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan

ROLES = ('start', 'first-of-link', 'inner', 'linkend', 'last')


def walk_shape(w, p, hdr2=None):
    """[(role, x word, y word)] for the level-1 shape at word p, or None if it does not close.
    A link header is val and id, plus a style word after a y with 0x2000 and an extra word
    after one with 0x1000; hdr2 forces a fixed size for every header after the first."""
    ln = w[p] & 0x1fff
    ex = 1 if w[p] & 0x2000 else 0
    if ln < 10 or p + ln > len(w):
        return None
    npts = w[p + 1] & 0x3fff
    out = [('start', w[p + 7 + ex], w[p + 8 + ex])]
    q = p + 9 + ex
    end = p + ln
    nlinks = 0
    while len(out) < npts:
        y = out[-1][2]
        h = 3 if nlinks == 0 else hdr2 or 2 + bool(y & 0x2000) + bool(y & 0x1000)
        if q + h > end:
            return None
        q += h
        nlinks += 1
        first = True
        while True:
            if q + 2 > end:
                return None
            x, y = w[q], w[q + 1]
            q += 2
            out.append(('first-of-link' if first else 'inner', x, y))
            first = False
            if len(out) == npts or y & 0x0800:
                break
        # the point that closed this link is a link end, or the shape's last point
        out[-1] = ('last' if len(out) == npts else 'linkend', out[-1][1], out[-1][2])
    if q + 1 != end:
        return None
    return out


def shapes_of(pl):
    w = struct.unpack_from('>%dH' % (len(pl) // 2), pl)
    if w[2] != 0x00fa or w[7] != 11:
        return []
    out = []
    for lay in range(12):
        off, cnt = w[23 + 2 * lay], w[24 + 2 * lay]
        if off == 0xffff:
            continue
        q = 22 + 2 * off
        for _ in range(cnt):
            if q + 9 > len(w) or w[q] == 0:
                break
            res = walk_shape(w, q) or walk_shape(w, q, 2) or walk_shape(w, q, 3)
            out.append(res)
            q += w[q] & 0x1fff
    return out


def run(paths):
    tally = {r: [0, 0] for r in ROLES}           # [bit clear, bit set]
    shapes = unparsed = twopt = twopt_clear = 0
    for path in paths:
        per = {r: [0, 0] for r in ROLES}
        with TileScan(path) as t:
            for r in t.records(section=1, level=1, even_only=True):
                pl = r.plain()
                if len(pl) < 6 or struct.unpack_from('>H', pl, 4)[0] != 0x00fa:
                    continue
                for s in shapes_of(pl):
                    if s is None:
                        unparsed += 1
                        continue
                    shapes += 1
                    if len(s) == 2:
                        twopt += 1
                        twopt_clear += 0 if s[1][1] & 0x0800 else 1
                    for role, x, y in s:
                        per[role][1 if x & 0x0800 else 0] += 1
        print('%-14s %s' % (os.path.basename(path),
                            '  '.join('%s %d/%d' % (r, per[r][1], per[r][0]) for r in ROLES)))
        for r in ROLES:
            tally[r][0] += per[r][0]
            tally[r][1] += per[r][1]
    print('TOTAL  shapes %d, unparsed %d, two-point %d (end point x-bit clear %d)'
          % (shapes, unparsed, twopt, twopt_clear))
    print('       role                 bit set    bit clear')
    exc = 0
    for r in ROLES:
        want_set = r in ('start', 'first-of-link', 'inner')
        bad = tally[r][0] if want_set else tally[r][1]
        exc += bad
        print('       %-14s %12d %12d   %s' % (r, tally[r][1], tally[r][0],
                                               'ok' if bad == 0 else 'EXCEPTIONS %d' % bad))
    print('       exceptions against the fixed pattern: %d' % exc)
    return exc


if __name__ == '__main__':
    sys.exit(1 if run(sys.argv[1:]) else 0)
