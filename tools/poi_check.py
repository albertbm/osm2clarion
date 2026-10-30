#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a POINT file's facilities, their search frames and their icons in the tiles.

    poi_check.py POINT.DAT [TILE.DAT ...] [--fold Þ=TH,þ=th,Ð=D,ð=d] [-v]

The POINT side, tiles or not: every facility (non-settlement name record) must be reachable
from the letter trie under each word of its folded name; slot 278 must hold one CATEGORY
entry per category present and slot 279 one SUB-CATEGORY entry per (category, sub) pair,
each with a trie over the facilities' keys and a record run of exactly their records; the
slot-15 nearby tree of each category must list exactly its facilities at their positions.
The tries are walked here, not through point_io's reader, and the parcel arithmetic is
GEOMETRY.md's formula rather than the writer's, so a shared mistake shows up.

The tile side: every 00ff point in a level-1 or level-2 background record must index a
12-byte side entry 0000 0000 ffff 0000 2xxx 0000 on a 6-halfword boundary whose code is the
sub-category, or the genre's fixed code (hospital 2000, rest area 3272), of a facility at
that parcel position; a facility inside a tile given here must have an icon at both levels,
and one whose parcel has no record at all is reported too. Level-2 positions follow from the
level-1 words as bg_tiles derives them. Works on factory tiles (mmap walk, no read_tile); a
factory tile has facilities the POINT file of one country does not, so there -v lists the
unmatched icons rather than failing.
"""
import collections
import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addre_io, clarion_bg, point_io  # noqa: E402
from tile_scan import TileScan  # noqa: E402

FIXED = {11: 2000, 30: 3272}        # genres without a sub-category: the side code they draw with
GENRE = {v: k for k, v in FIXED.items()}
SETTLEMENT = 0x1f


def genre_of(code):
    if code in GENRE: return GENRE[code]
    if 900 <= code < 1000: return 6
    if 3500 <= code < 3700: return 5
    if 400 <= code < 500: return 2
    return None


def rowcol(level, w0, w1):
    """GEOMETRY.md: the parcel code's row and column at level 1 (64 per mesh unit) or 2 (16)."""
    R, C = (w0 >> 8) & 0x7f, (w0 >> 15) << 8 | w1 >> 8
    lo0, lo1 = w0 & 0xff, w1 & 0xff
    if level == 2:
        return R * 16 + (lo0 >> 4), C * 16 + (lo1 >> 4)
    return R * 64 + ((lo0 >> 4) << 2 | lo0 & 3), C * 64 + ((lo1 >> 4) << 2 | lo1 & 3)


def tile_box(name):
    """(lat0, lat1, lon0, lon1) of a tile from its name: lat_min = B * 2/3, lon_max = 108 - R, 8 units each."""
    B, R = int(name[1:3], 16), int(name[4:7], 16)
    return B * 2 / 3, (B + 8) * 2 / 3, 108 - R - 8, 108 - R


def walk_trie(d, base, cr, kw):
    """[(key, first record index, count)] of a trie at file offset base, 20-word records."""
    def crec(o):
        return (struct.unpack_from('>I', d, o)[0], struct.unpack_from('>H', d, o + 4)[0],
                d[o + 8:o + 8 + kw], int.from_bytes(d[o + 8 + kw:o + cr], 'big'))
    o, root = base, []
    while struct.unpack_from('>I', d, o)[0]:
        root.append(crec(o))
        o += cr
    out, stack = [], [(c, b'') for c in root]
    while stack:
        c, pfx = stack.pop()
        k = pfx + c[2]
        if c[1] == 0:
            out.append((k, c[0] // 20, c[3]))
            continue
        fo = base + 2 * c[0]
        n = c[1] * 2 // cr - 1
        own = int.from_bytes(d[fo + n * cr + 8 + kw:fo + (n + 1) * cr], 'big')
        if own:
            out.append((k, struct.unpack_from('>I', d, fo + n * cr)[0] // 20, own))
        stack += [(crec(fo + i * cr), k) for i in range(n)]
    return sorted(out)


def folded_words(name, fold):
    """The upper-cased folded name and its word starts, as the trie keys are cut."""
    s = ' '.join(addre_io.fold_key(name.translate(str.maketrans('.+', '  ', '"')), fold).upper().split())
    starts, p = [], 0
    for w in s.split(' '):
        if p == 0 or (len(w) >= 2 and w[0].isalnum()):
            starts.append(p)
            if len(w) >= 4 and w[1] == "'":
                starts.append(p + 2)
        p += len(w) + 1
    return s, starts


def check_trie(d, base, cr, kw, rec_at, nrec, want, reach, tag, problems):
    """One trie and record run: every record in range and naming one of `want` (name index
    -> fields), every key a word-start suffix of its records' folded names, and every name in
    `reach` reachable under each of its words."""
    seen = collections.defaultdict(set)
    for k, start, n in walk_trie(d, base, cr, kw):
        key = bytes(k[i] for i in range(0, len(k), kw)).decode('latin-1') if kw > 1 else k.decode('latin-1')
        if start + n > nrec:
            problems.append('%s: key %r points past the record run (%d+%d of %d)' % (tag, key, start, n, nrec))
            continue
        for i in range(start, start + n):
            r = d[rec_at + 40 * i:rec_at + 40 * (i + 1)]
            ni = struct.unpack_from('>H', r, 30)[0]
            if ni not in want:
                problems.append('%s: key %r record %d is name %d, not a facility of this frame' % (tag, key, i, ni))
                continue
            s, starts = want[ni]['words']
            if not any(s.startswith(key, p) for p in starts):
                problems.append('%s: key %r is not a word start of %r' % (tag, key, want[ni]['name']))
            seen[ni].add(key)
    for ni in reach:
        s, starts = want[ni]['words']
        for p in starts:
            if s[p:] not in seen[ni]:
                problems.append('%s: %r is not reachable under %r' % (tag, want[ni]['name'], s[p:]))


def check_point(d, fold, problems, stats):
    """The POINT side; returns the facilities as {name index: fields}."""
    p = point_io.Point(d)
    fac = {}
    for i, nr in enumerate(p.names):
        f = point_io.parse_name_record(nr)
        if f['category'] == SETTLEMENT:
            continue
        f['words'] = folded_words(f['name'], fold)
        if not f['words'][0].isascii():
            problems.append('name %d %r folds to a non-ASCII key' % (i, f['name']))
        fac[i] = f
    stats['facilities'] = len(fac)
    if not fac:
        return fac
    # the main trie, over everything the file names
    everything = dict(fac)
    for i, nr in enumerate(p.names):
        if i not in fac:
            f = point_io.parse_name_record(nr)
            f['words'] = folded_words(f['name'], fold)
            everything[i] = f
    check_trie(d, 0x1100, p.cr, p.kw, p.rec_off, p.nrec, everything, fac, 'main trie', problems)
    # the CATEGORY and SUB-CATEGORY tables
    cats = {f['category'] for f in fac.values()}
    pairs = {(f['category'], f['subcategory']) for f in fac.values() if f['subcategory'] != 0xffff}
    for slot, label, keyof, present in ((278, 'CATEGORY', lambda t: t[2], cats),
                                        (279, 'SUB-CATEGORY', lambda t: t[2:], pairs)):
        table = point_io.category_table(d, slot)
        got = [keyof(e['tail']) for e in table]
        if got != sorted(present):
            problems.append('slot %d: %s entries %s, facilities need %s' % (slot, label, got, sorted(present)))
        for e in table:
            if e['tail'][:2] != (p.area, 0xffff):
                problems.append('slot %d: entry %s tail starts %s, not (area %d, ffff)' % (slot, keyof(e['tail']), e['tail'][:2], p.area))
            k = keyof(e['tail'])
            want = {i: f for i, f in fac.items() if (f['category'] if slot == 278 else (f['category'], f['subcategory'])) == k}
            runs = {struct.unpack_from('>H', d, e['rec_at'] + 40 * i + 30)[0] for i in range(e['nrec'])}
            if runs != set(want):
                problems.append('slot %d %s: record run names %s, facilities %s' % (slot, k, sorted(runs), sorted(want)))
            check_trie(d, e['trie_at'], e['cr'], e['kw'], e['rec_at'], e['nrec'], want, want, '%s %s' % (label, k), problems)
        stats['slot %d entries' % slot] = len(table)
    # the slot-15 nearby trees
    a, _n = struct.unpack_from('>IH', d, 0x800 + 6 * 15)
    if a == 0xffffffff:
        problems.append('slot 15: no nearby table')
        return fac
    t15, codes = addre_io.unaddr(a), []
    for e in range(1000):
        w = struct.unpack_from('>HIIHHHHHHHHH', d, t15 + 28 * e)
        items, _sub = point_io.parse_nearby(d, addre_io.unaddr(w[1]), (w[5] - 5) // 8)
        codes.append(w[11])
        # one leaf item per facility position (the factory lists a facility's language rows once)
        mine = {i for i, f in fac.items() if f['category'] == w[11]}
        want = {(f['lat'], f['lon'], f['x'], f['y']) for i, f in fac.items() if i in mine}
        got = {(it[3], it[4], it[5] & 0x7ff, it[6]) for it in items}
        if got != want:
            problems.append('slot 15 category %d: nearby positions %s, facilities at %s' % (w[11], sorted(got - want), sorted(want - got)))
        strays = sorted({it[0] for it in items} - mine)
        if strays:
            problems.append('slot 15 category %d: items name %s, not facilities of the category' % (w[11], strays))
        if w[0] == 0xffff:
            break
    if codes != sorted(cats):
        problems.append('slot 15: categories %s, facilities need %s' % (codes, sorted(cats)))
    stats['slot 15 entries'] = len(codes)
    return fac


def icon_targets(fac):
    """{level: {(w0, w1, x, y): Counter(code)}}; two level-1 positions can share one level-2
    position, so codes are counted, not collected."""
    out = {1: collections.defaultdict(collections.Counter), 2: collections.defaultdict(collections.Counter)}
    for f in fac.values():
        code = f['subcategory'] if f['subcategory'] != 0xffff else FIXED.get(f['category'])
        if code is None:
            continue
        row, col = rowcol(1, f['lat'], f['lon'])
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


def check_tiles(tiles, fac, stats, problems):
    want = icon_targets(fac)
    expected = {lv: sum(sum(c.values()) for c in want[lv].values()) for lv in (1, 2)}
    parcels_seen = {1: set(), 2: set()}
    per_tile = collections.Counter()
    boxes = []
    for path in tiles:
        name = os.path.basename(path)
        boxes.append(tile_box(name))
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
                n = sum(stats[(r.level, k)] for k in ('matched', 'no facility', 'wrong code'))
                check_record(plain, want, r.level, stats, problems, '%s L%d %04x %04x' % (name, r.level, w0, w1))
                n2 = sum(stats[(r.level, k)] for k in ('matched', 'no facility', 'wrong code'))
                if n2 > n:
                    per_tile[(name, r.level)] += 1
                    per_tile[(name, r.level, 'icons')] += n2 - n

    def covered(w0, w1, lv):
        row, col = rowcol(lv, w0, w1)
        per = 48 if lv == 1 else 12, 32 if lv == 1 else 8
        lat, lon = (row + 0.5) / per[0], (col + 0.5) / per[1] - 924
        return any(b[0] <= lat < b[1] and b[2] <= lon < b[3] for b in boxes)
    for lv in (1, 2):
        for key, codes in want[lv].items():
            left = sorted(codes.elements())
            if not left:
                continue
            if key[:2] in parcels_seen[lv]:
                stats[(lv, 'missing icon')] += len(left)
                problems.append('L%d %04x %04x: facility code(s) %s at %d,%d has no icon' % (lv, *key[:2], left, *key[2:]))
            elif covered(*key[:2], lv):
                stats[(lv, 'no record')] += len(left)
                problems.append('L%d %04x %04x: facility code(s) %s at %d,%d: the parcel has no record in its tile' % (lv, *key[:2], left, *key[2:]))
            else:
                stats[(lv, 'outside')] += len(left)
    for lv in (1, 2):
        cats = ' '.join('%s:%d' % (g, stats[(lv, g)]) for g in (5, 6, 11, 30, 2, None) if stats[(lv, g)])
        print('level %d: icons %d (%s), matched %d, no facility %d, wrong code %d, missing icon %d, no record %d, '
              'outside the tiles %d, of %d facilities in %d parcels'
              % (lv, sum(stats[(lv, g)] for g in (5, 6, 11, 30, 2, None)), cats, stats[(lv, 'matched')],
                 stats[(lv, 'no facility')], stats[(lv, 'wrong code')], stats[(lv, 'missing icon')],
                 stats[(lv, 'no record')], stats[(lv, 'outside')], expected[lv], len(parcels_seen[lv])))
    for (name, lv), n in sorted(k for k in per_tile.items() if len(k[0]) == 2):
        print('  %s level %d: %d records carry %d icons' % (name, lv, n, per_tile[(name, lv, 'icons')]))


def main(argv):
    verbose = argv.count('-v')        # -v lists 200 problems and exits 0, -vv lists them all
    argv = [a for a in argv if a != '-v']
    fold = None
    if '--fold' in argv:
        i = argv.index('--fold')
        fold = dict(kv.split('=') for kv in argv[i + 1].split(','))
        del argv[i:i + 2]
    point_path, tiles = argv[0], argv[1:]
    stats = collections.Counter()
    problems = []
    fac = check_point(open(point_path, 'rb').read(), fold, problems, stats)
    print('POINT: %d facilities, slot 278 %s entries, slot 279 %s entries, slot 15 %s entries'
          % (stats['facilities'], stats['slot 278 entries'], stats['slot 279 entries'], stats['slot 15 entries']))
    if tiles:
        check_tiles(tiles, fac, stats, problems)
    print('problems', len(problems))
    for p in problems[:{0: 20, 1: 200}.get(verbose, len(problems))]:
        print('  ' + p)
    return 1 if problems and not verbose else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
