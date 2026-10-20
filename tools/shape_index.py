#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where every drawn road shape sits inside the level-1 0x00fa records of a tile.

An address record names a point by its word offset from the record's geometry unit
(REPORT-kanren in leaf-is-map); rd_build.py --shape-index writes the sidecar this checks
tiles against. Link ids restart at 1 per class, so (cls, id) names a link within a record."""
import json, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, clarion_tile, fa_encode


def inflate(raw):
    return zlib.decompress(raw) if raw[:2] == b'\x58\x85' else raw


def label_name(label):
    """The street name of a `1=NAME;2=REF` label, or the label itself."""
    part = label.split(';')[0]
    return part[2:] if part.startswith('1=') else part


def bg_labels(bg):
    r = clarion_bg.parse_record(bg)
    s = r['sections'].get('names')
    out = {}
    if not s: return out
    nl = struct.unpack_from('>H', s, 2)[0]; p = 24
    for _ in range(nl):
        if p + 10 > len(s): break
        typ, flags = struct.unpack_from('>HH', s, p)
        q = p + 8 + (2 if flags & 0x4000 else 0); ln = struct.unpack_from('>H', s, q)[0]
        out[p // 2] = s[q + 2:q + 2 + ln].rstrip(b'\0').decode('utf-8', 'replace')
        p = q + 2 + ln + (ln & 1)
    return out


def tile_records(d):
    t = clarion_tile.read_tile(d)
    out = {}
    for sec in t['sections']:
        if sec['id'] != 1: continue
        for L in sec['levels']:
            if L['level'] != 1: continue
            for D in L['dirs']:
                for s in D['slots'].values():
                    byk = {r['k']: r for r in s['recs']}
                    for k, r in byk.items():
                        if k % 2: continue
                        plain = inflate(r['raw'])
                        if len(plain) < 44 or struct.unpack_from('>H', plain, 4)[0] != 0x00fa: continue
                        bg = inflate(byk[k + 1]['raw']) if k + 1 in byk else None
                        out[struct.unpack_from('>HH', plain, 0)] = (plain, bg)
    return out


def shapes_of(plain, bg):
    code, unit, shapes = fa_encode.parse_record(plain)
    names = bg_labels(bg) if bg else {}
    for s in shapes: s['label'] = names.get(s['name'], '')
    return code, unit, shapes


def verify(side, d):
    """[] when every indexed word matches the tile: same shape, label, class, points."""
    recs = tile_records(d); bad = []
    for R in side['records']:
        code = tuple(R['code'])
        if code not in recs:
            bad.append('%04x %04x: no 0x00fa record' % code); continue
        _, unit, shapes = shapes_of(*recs[code])
        if unit != R['unit']: bad.append('%04x %04x: unit %d, index says %d' % (code + (unit, R['unit'])))
        for S in R['shapes']:
            got = next((s for s in shapes if s['at'] == S['at']), None)
            if got is None:
                bad.append('%04x %04x: no shape at word %d' % (code + (S['at'],))); continue
            if got['label'] != S['label']:
                bad.append('%04x %04x @%d: label %r, index says %r' % (code + (S['at'], got['label'], S['label'])))
            if 'cls' in S and got['cls'] != S['cls']:
                bad.append('%04x %04x @%d: class %d, index says %d' % (code + (S['at'], got['cls'], S['cls'])))
            for P in S['points']:
                if got['points'].get(P['word']) != (P['role'], P['link']):
                    bad.append('%04x %04x @%d: word %d is %s, index says %s link %d'
                               % (code + (S['at'], P['word'], got['points'].get(P['word']), P['role'], P['link'])))
            if len(got['points']) != len(S['points']):
                bad.append('%04x %04x @%d: %d points, index has %d' % (code + (S['at'], len(got['points']), len(S['points']))))
    return bad


def main(argv):
    if argv[:1] == ['verify']:
        side = json.load(open(argv[1])); bad = verify(side, open(argv[2], 'rb').read())
        n = sum(len(S['points']) for R in side['records'] for S in R['shapes'])
        print('%s: %d records, %d shapes, %d points; %d problems'
              % (argv[2], len(side['records']), sum(len(R['shapes']) for R in side['records']), n, len(bad)))
        for b in bad: print('  ' + b)
        return 1 if bad else 0
    if argv[:1] == ['dump']:
        for code, (plain, bg) in sorted(tile_records(open(argv[1], 'rb').read()).items()):
            _, unit, shapes = shapes_of(plain, bg)
            print('%04x %04x unit %d, %d shapes' % (code + (unit, len(shapes))))
            for s in shapes:
                print('  @%-5d layer %d %-28s links %s points %d%s' % (s['at'], s['layer'], s['label'], s['links'],
                                                                       len(s['points']), '' if s['ok'] else ' (parse)'))
        return 0
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
