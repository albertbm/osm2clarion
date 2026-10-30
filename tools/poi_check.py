#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a tile's facility icons against a POINT file.

    poi_check.py POINT.DAT TILE.DAT [TILE...] [-v]

Every 00ff point in a level-1 or level-2 background record must index a 12-byte side entry
0000 0000 ffff 0000 2xxx 0000 on a 6-halfword boundary, and its code must be the sub-category,
or the genre's fixed code (hospital 2000, rest area 3272), of a POINT facility at that parcel
position; a facility whose parcel the tiles hold must have an icon at both levels. Level-2
positions come from the level-1 words, the way bg_tiles derives them. Works on our tiles and
on factory ones (mmap walk, no read_tile); a factory tile has facilities the POINT file of one
country does not, so there -v shows which icons go unmatched rather than failing.
"""
import collections
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, point_io  # noqa: E402
from tile_scan import TileScan  # noqa: E402

GENRE = {2000: 11, 3272: 30}


def genre_of(code):
    if code in GENRE: return GENRE[code]
    if 900 <= code < 1000: return 6
    if 3500 <= code < 3700: return 5
    if 400 <= code < 500: return 2
    return None


def facilities(d):
    """{level: {(w0, w1, x, y): Counter(code)}} of every non-settlement name record; two
    level-1 positions can share one level-2 position, so codes are counted, not collected."""
    out = {1: collections.defaultdict(collections.Counter), 2: collections.defaultdict(collections.Counter)}
    for nr in point_io.Point(d).names:
        f = point_io.parse_name_record(nr)
        if f['category'] == point_io.SETTLEMENT:
            continue
        code = f['subcategory'] if f['subcategory'] != 0xffff else clarion_bg.ICON_CODES.get(f['category'])
        if code is None:
            continue
        row, col = clarion_bg.parcel_rowcol(1, f['lat'], f['lon'])
        out[1][(f['lat'], f['lon'], f['x'], f['y'])][code] += 1
        X, Y = col * 2048 + f['x'], row * 2048 + f['y']
        out[2][clarion_bg.parcel_code(2, Y // 8192, X // 8192) + ((X // 4) % 2048, (Y // 4) % 2048)][code] += 1
    return out


def check_record(plain, want, level, stats, problems, tag):
    w = struct.unpack_from('>22H', plain, 0)
    try:
        rec = clarion_bg.parse_record(plain)
    except AssertionError as e:
        problems.append('%s: unparsed record (%s)' % (tag, e))
        return
    g = rec['layers'].get(25)
    if not g:
        return
    if 'raw' in g:
        problems.append('%s: layer 25 not parsed (%s)' % (tag, g.get('note')))
        return
    side = rec['sections'].get('points', b'')
    for s in [g] + g.get('more', []):
        for sh in s['shapes']:
            if sh['type'] != clarion_bg.ICON:
                continue
            x, y = sh['pts'][0]
            if 'idx' not in sh:
                problems.append('%s: 00ff point at %d,%d without a side index' % (tag, x, y))
                continue
            idx = sh['idx']
            if idx % 6 or 2 * idx + 12 > len(side):
                problems.append('%s: idx %d off the side section (%d bytes)' % (tag, idx, len(side)))
                continue
            e = struct.unpack_from('>6H', side, 2 * idx)
            if e[:4] != (0, 0, 0xffff, 0) or e[4] & 0xf000 != 0x2000 or e[5]:
                problems.append('%s: side entry %s is not an icon entry' % (tag, ' '.join('%04x' % v for v in e)))
                continue
            code = e[4] & 0xfff
            stats[(level, genre_of(code))] += 1
            codes = want[level].get((w[0], w[1], x, y))
            if codes is None:
                stats[(level, 'no facility')] += 1
                problems.append('%s: icon code %d at %d,%d has no POINT facility there' % (tag, code, x, y))
            elif not codes[code]:
                stats[(level, 'wrong code')] += 1
                problems.append('%s: icon code %d at %d,%d but POINT has %s' % (tag, code, x, y, sorted(codes.elements())))
            else:
                stats[(level, 'matched')] += 1
                codes[code] -= 1


def main(argv):
    verbose = '-v' in argv
    if verbose:
        argv.remove('-v')
    point_path, tiles = argv[0], argv[1:]
    want = facilities(open(point_path, 'rb').read())
    expected = {lv: sum(sum(c.values()) for c in want[lv].values()) for lv in (1, 2)}
    stats = collections.Counter()
    problems = []
    parcels_seen = {1: set(), 2: set()}
    per_tile = collections.Counter()
    for path in tiles:
        name = os.path.basename(path)
        with TileScan(path) as t:
            for r in t.records(section=1):
                if r.level not in (1, 2) or not r.is_zlib:
                    continue
                try:
                    plain = r.plain()
                except zlib.error:
                    continue
                if len(plain) < 44 or struct.unpack_from('>H', plain, 4)[0] != 0x19:
                    continue
                w0, w1 = struct.unpack_from('>HH', plain, 0)
                parcels_seen[r.level].add((w0, w1))
                before = len(problems)
                n = sum(stats[(r.level, k)] for k in ('matched', 'no facility', 'wrong code'))
                check_record(plain, want, r.level, stats, problems, '%s L%d %04x %04x' % (name, r.level, w0, w1))
                n2 = sum(stats[(r.level, k)] for k in ('matched', 'no facility', 'wrong code'))
                if n2 > n:
                    per_tile[(name, r.level)] += 1
                    per_tile[(name, r.level, 'icons')] += n2 - n
    for lv in (1, 2):
        for key, codes in want[lv].items():
            left = sorted(codes.elements())
            if left and key[:2] in parcels_seen[lv]:
                stats[(lv, 'missing icon')] += len(left)
                problems.append('L%d %04x %04x: facility code(s) %s at %d,%d has no icon' % (lv, *key[:2], left, *key[2:]))
    for lv in (1, 2):
        cats = ' '.join('%s:%d' % (g, stats[(lv, g)]) for g in (5, 6, 11, 30, 2, None) if stats[(lv, g)])
        print('level %d: icons %d (%s), matched %d, no facility %d, wrong code %d, missing icon %d of %d facilities in %d parcels'
              % (lv, sum(stats[(lv, g)] for g in (5, 6, 11, 30, 2, None)), cats, stats[(lv, 'matched')],
                 stats[(lv, 'no facility')], stats[(lv, 'wrong code')], stats[(lv, 'missing icon')], expected[lv],
                 len(parcels_seen[lv])))
    for (name, lv), n in sorted(k for k in per_tile.items() if len(k[0]) == 2):
        print('  %s level %d: %d records carry %d icons' % (name, lv, n, per_tile[(name, lv, 'icons')]))
    print('problems', len(problems))
    for p in problems[:200 if verbose else 20]:
        print('  ' + p)
    return 1 if problems and not verbose else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
