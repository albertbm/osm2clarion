#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow every record of a card's X08-02 POINT files into the card's road tiles.

A FACILITY record's references (tag, flag, v) point at the x word unit + v of a point in
the level-1 0x00fa record its (GLAT, GLON) names. For each record this reads the card's own
tile and checks:

    lands      both words are point x words of one shape, at the ends of one link
    flag       0x40 on a graph node (shape start or link end), 0x00 on an interior vertex
    order      tags 2/3 ascend, 4/5 descend
    on link    a facility (any category but 31, settlements) sits within ON_LINK_M of
               that link, in the parcel its references are in
    search     every record has a row under a key its folded name starts with; every row
               carries its record's ladder, category and sub-category; the CATEGORY and
               SUB CATEGORY runs are the main rows of theirs in main order; the nearby trees
               hold each category's records

Reads tiles with clarion_tile.read_tile, so it is for our own cards: whole parcels only.

    point_resolve.py CARD.img [--verbose] [--only POINT007.DAT ...]
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, point_v5, poi_point, shape_index
from addr_resolve import Card, code_rc, unpack_cmp

ON_LINK_M = 3.0


def point_files(card, only=()):
    out = {}
    for p in sorted(card.files):
        d, n = os.path.split(p)
        if d != 'REFER001' or not n.startswith('POINT'): continue
        if only and n.split('.DAT')[0] + '.DAT' not in only: continue
        if n.endswith('.DAT'): out[n] = card.read(p)
        elif n.endswith('.DAT.cmp'): out[n[:-4]] = unpack_cmp(card.read(p), card.read(p[:-4] + '.ind'))
    return out


def resolve(card, f):
    """Failures of one record's references and position."""
    rc = code_rc(f['glat'], f['glon'])
    if rc is None: return ['sub-parcel code %04x %04x' % (f['glat'], f['glon'])]
    tile, recs = card.records(*rc)
    code = clarion_bg.parcel_code(1, *rc)
    if code not in recs: return ['no 0x00fa record %04x %04x' % code]
    plain, bg = recs[code]
    _, unit, shapes = shape_index.shapes_of(plain, bg, card.codec[tile])
    bad, hit = [], []
    if len(f['refs']) != 2: bad.append('%d reference groups, not 2' % len(f['refs']))
    for g in f['refs']:
        tag, flag, v = g
        w = unit + v
        s = next((s for s in shapes if s['at'] <= w < s['at'] + s['length']), None)
        role = s['points'].get(w) if s else None
        if tag not in (2, 3, 4, 5): bad.append('group %s: tag not decoded' % (g,))
        if role is None: bad.append('group %s: word %d is not a point x word' % (g, w)); continue
        if (flag == 0x40) != (role[0] in ('start', 'end')): bad.append('group %s: flag 0x%02x on a %s' % (g, flag, role[0]))
        hit.append((g, s, role, w))
    if bad or len(hit) != 2: return bad
    (ga, sa, ra, wa), (gb, sb, rb, wb) = hit
    if sa is not sb: return ['the two groups are on two shapes']
    up = ga[0] in (2, 3) and gb[0] in (2, 3) and wb > wa
    down = ga[0] in (4, 5) and gb[0] in (4, 5) and wb < wa
    if not (up or down): bad.append('order does not match the tags')
    lo_w, hi_w = min(wa, wb), max(wa, wb)
    end, start = (rb, ra) if wb > wa else (ra, rb)
    inner = [r for q, r in sa['points'].items() if lo_w < q < hi_w]
    if end[0] != 'end' or start[0] == 'vertex' or any(r[1] != end[1] for r in inner) or \
            (start[0] == 'start' and start[1] != end[1]):
        bad.append('the groups are not the two ends of one link')
    if f['category'] != point_v5.SETTLEMENT:
        if (f['lat'], f['lon']) != (f['glat'], f['glon']): bad.append('facility outside its referenced parcel')
        lat = (rc[0] + f['y'] / 2048) / 48
        mx, my = poi_point.parcel_metres(lat)
        ws = struct.unpack('>%dH' % (len(plain) // 2), plain)
        run = [q for q in sorted(sa['points']) if min(wa, wb) <= q <= max(wa, wb)]
        x, y = f['xw'] & 0x7ff, f['y']
        d = min(poi_point.seg_dist(x * mx, y * my, (ws[a] & 0x7ff) * mx, (ws[a + 1] & 0x7ff) * my,
                                   (ws[b] & 0x7ff) * mx, (ws[b + 1] & 0x7ff) * my)[0] for a, b in zip(run, run[1:]))
        if d > ON_LINK_M: bad.append('%.1f m from its link' % d)
    return bad


def search_problems(P, fields):
    """Failures of the search frames against the records."""
    bad = []
    n = len(fields)
    whole = [0] * n
    rows = [point_v5.parse_row(r) for r in P.rows]
    for u, start, cnt in P.keys:
        for r in rows[start:start + cnt]:
            i = r['name_idx']
            if i >= n: bad.append('row to record %d of %d' % (i, n)); continue
            f = fields[i]
            if (r['levels'][:len(f['levels'])], r['category'], r['subcategory']) != (f['levels'], f['category'], f['subcategory']):
                bad.append('row of %r does not carry its record' % f['name'])
            key = point_v5.search_key(point_v5.text(f['name']).decode('latin-1')).encode('latin-1')
            whole[i] += key.startswith(u)
    bad += ['%r: no row under a key of its whole name' % fields[i]['name'] for i in range(n) if not whole[i]]
    order = [point_v5.parse_row(r)['name_idx'] for u, s, c in P.keys for r in P.rows[s:s + c]]
    for (tail, keys, crows), what in [(c, 'category') for c in P.cats] + [(s, 'sub') for s in P.subs]:
        sel = (lambda f, t=tail: f['category'] == t[1]) if what == 'category' else \
              (lambda f, t=tail: (f['category'], f['subcategory']) == t[1:])
        want = [i for i in order if sel(fields[i])]
        got = [point_v5.parse_row(r)['name_idx'] for u, s, c in keys for r in crows[s:s + c]]
        if got != want: bad.append('%s run %s holds %d rows, not the %d main rows of it' % (what, tail, len(got), len(want)))
    for w, items, _sub in P.near:
        code = w[11]
        recs = {i for i, f in enumerate(fields) if f['category'] == code}
        for it in items:
            f = fields[it[0]]
            if it[0] not in recs or (it[3], it[4], it[5] & 0x7ff, it[6]) != (f['lat'], f['lon'], f['xw'] & 0x7ff, f['y']):
                bad.append('nearby %d item %d is not its record' % (code, it[0]))
        names = {(f['name'], f['lat'], f['lon'], f['xw'], f['y']) for i, f in enumerate(fields) if i in recs}
        if len(items) != len(names): bad.append('nearby %d: %d items for %d places' % (code, len(items), len(names)))
    for f in fields:
        if f['tel'][0] != 0xffff and f['tel'][1] >= len(P.infos): bad.append('%r: telephone %d of %d' % (f['name'], f['tel'][1], len(P.infos)))
    return bad


def main(argv):
    if not argv: sys.exit(__doc__)
    card = Card(argv[0]); verbose = '--verbose' in argv
    only = argv[argv.index('--only') + 1:] if '--only' in argv else ()
    total = ok = fails = 0
    for fn, d in point_files(card, only).items():
        P = point_v5.Point(d)
        fields = [point_v5.parse_name_record(r) for r in P.names]
        good = 0
        for f in fields:
            bad = resolve(card, f)
            good += not bad
            if bad or verbose:
                print('  %-32s cat %3d %04x %04x %s' % (point_v5.text(f['name']).decode('latin-1'), f['category'],
                                                         f['glat'], f['glon'], f['refs']))
                for b in bad: print('      FAIL ' + b)
        sp = search_problems(P, fields)
        for b in sp[:20]: print('  FAIL ' + b)
        print('%s: %d of %d records land on a link of our roads; %d search-frame problems' % (fn, good, len(fields), len(sp)))
        total += len(fields); ok += good; fails += len(sp)
    passed = total and ok == total and not fails
    print('all: %d of %d (%s)' % (ok, total, 'PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    import cli_help; cli_help.help_exit(__doc__)
    sys.exit(main(sys.argv[1:]))
