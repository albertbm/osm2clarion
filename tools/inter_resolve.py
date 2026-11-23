#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow every crossing of a card's INTER files into the card's road tiles.

A 2ND-STREET record's (LA, LO) is a level-1 parcel code and (x, y) a point inside it. For
each record this reads the card's own tile and checks:

    junction   a shape labelled with the first street and one labelled with the second
               both have a graph node (shape start or link end) on that exact point
    run        each 1ST-STREET record's crossings follow the previous one's, flags 0x10
               on the first of the run and 0x11 after it
    keys       every 1ST-STREET record has one whole-name row under a key that starts its
               folded name, and every row carries its record's ladder

Reads tiles with clarion_tile.read_tile, so it is for our own cards: whole parcels only.

    inter_resolve.py CARD.img [--verbose] [--only INTER007.DAT ...]
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, inter_io, shape_index
from addr_resolve import Card, code_rc, unpack_cmp


def inter_files(card, only=()):
    out = {}
    for p in sorted(card.files):
        d, n = os.path.split(p)
        if d != 'REFER001' or not n.startswith('INTER'): continue
        if only and n.split('.DAT')[0] + '.DAT' not in only: continue
        if n.endswith('.DAT'): out[n] = card.read(p)
        elif n.endswith('.DAT.cmp'): out[n[:-4]] = unpack_cmp(card.read(p), card.read(p[:-4] + '.ind'))
    return out


class Nodes:
    """{(x, y): {street name}} of the graph nodes of named shapes, per level-1 parcel."""

    def __init__(self, card):
        self.card = card
        self.cache = {}

    def __call__(self, la, lo):
        if (la, lo) in self.cache: return self.cache[(la, lo)]
        rc = code_rc(la, lo); out = None
        if rc is not None:
            tile, recs = self.card.records(*rc)
            code = clarion_bg.parcel_code(1, *rc)
            if code in recs:
                plain, bg = recs[code]
                _, _, shapes = shape_index.shapes_of(plain, bg, self.card.codec[tile])
                w = struct.unpack('>%dH' % (len(plain) // 2), plain)
                out = {}
                for s in shapes:
                    if not s['label'].startswith('1='): continue
                    name = shape_index.label_name(s['label']).upper()
                    for word, (role, _) in s['points'].items():
                        if role != 'vertex':
                            out.setdefault((w[word] & 0x7ff, w[word + 1] & 0x7ff), set()).add(name)
        self.cache[(la, lo)] = out
        return out


def check_file(card, nodes, d, verbose):
    """(records, good records, structural failures) of one INTER file."""
    I = inter_io.Inter(d)
    firsts = [inter_io.parse_first(r) for r in I.first]
    bad = []
    at = 0
    n = good = 0
    for f in firsts:
        if f['second_at'] != at: bad.append('%s: crossings start at %d, not %d' % (f['name'], f['second_at'], at))
        at = f['second_at'] + f['count']
        for j, r in enumerate(I.second[f['second_at']:at]):
            s = inter_io.parse_second(r)
            n += 1
            why = []
            if s['more'] != (j > 0): why.append('run flag 0x%02x' % (r[0] >> 3))
            pt = nodes(s['la'], s['lo'])
            if pt is None: why.append('no 0x00fa record %04x %04x' % (s['la'], s['lo']))
            else:
                here = pt.get((s['x'], s['y']), set())
                for street in (f['name'], s['name']):
                    if shape_index.label_text(street.upper()) not in here: why.append('no node of %s at the point (%s)' % (street, ', '.join(sorted(here)) or 'no named node'))
            good += not why
            if why or verbose:
                print('  %-28s x %-28s %04x %04x %4d %4d' % (f['name'], s['name'], s['la'], s['lo'], s['x'], s['y']))
                for w in why: print('      FAIL ' + w)
    if at != len(I.second): bad.append('%d crossings, the runs cover %d' % (len(I.second), at))
    whole = [0] * len(firsts)
    for u, start, cnt in I.keys:
        for r in I.rows[start:start + cnt]:
            p = inter_io.parse_row(r)
            i = p['first_idx']
            if i >= len(firsts): bad.append('row to record %d of %d' % (i, len(firsts))); continue
            lv = firsts[i]['levels']
            if p['levels'][:len(lv)] != tuple(lv): bad.append('row of %s carries ladder %s, not %s' % (firsts[i]['name'], p['levels'], lv))
            if not p['later']:
                whole[i] += 1
                if not inter_io.search_key(firsts[i]['name']).encode('latin-1').startswith(u):
                    bad.append('key %r does not start %s' % (u, firsts[i]['name']))
    bad += ['%s: %d whole-name rows' % (f['name'], c) for f, c in zip(firsts, whole) if c != 1]
    return n, good, bad


def main(argv):
    if not argv: sys.exit(__doc__)
    card = Card(argv[0]); verbose = '--verbose' in argv
    only = argv[argv.index('--only') + 1:] if '--only' in argv else ()
    nodes = Nodes(card)
    total = ok = 0; fails = 0
    for fn, d in inter_files(card, only).items():
        n, good, bad = check_file(card, nodes, d, verbose)
        for b in bad[:20]: print('  FAIL ' + b)
        print('%s: %d of %d crossings land on a junction of both streets; %d structural problems' % (fn, good, n, len(bad)))
        total += n; ok += good; fails += len(bad)
    passed = total and ok == total and not fails
    print('all: %d of %d (%s)' % (ok, total, 'PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    import cli_help; cli_help.help_exit(__doc__)
    sys.exit(main(sys.argv[1:]))
