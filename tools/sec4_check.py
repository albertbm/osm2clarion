#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the offsets that tie a parcel's section-4 record to its 0x00fa road record.

A wrong one hangs guidance in the car, so every link is walked both ways: ring keys land on
a node record at the same x,y and come back round; each G names a sub-record whose node
key names that node back; each (flag, ref) names the node record next along the approach
link string; sub-records come in key order with a contiguous chain and a text block the
header agrees with. Works on our tiles and on factory ones (mmap walk, no read_tile).

    sec4_check.py tile.DAT [--sidecar side.json] [-v] [--factory]

--factory relaxes the one check factory data does not meet, that every occurrence of an
x,y is on one ring; it keeps a flyover's two nodes apart.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fa_encode, sec4_write
from tile_scan import TileScan

def key_base(plain):
    """Node keys and refs count record words from the geometry unit, 2 * w7 (22 in every record we write)."""
    return 2 * struct.unpack_from('>H', plain, 14)[0]


def on_edge(pt):
    return pt[1] in (0, 2047) or pt[2] in (0, 2047)


def nodes_of(strings):
    """{x-word offset: (string index, point index)} for every node record."""
    out = {}
    for si, s in enumerate(strings):
        for k, pt in enumerate(s['pts']):
            if pt[5] is not None: out[pt[0]] = (si, k)
    return out


def ring_problems(strings, nodes, base, strict=True):
    """Identical-node rings: every key lands on a node record with the same x,y; following
    keys returns to the start within 10 hops; and (strict) every occurrence of a point is on
    its ring. A parcel-edge node may point out of the record: the factory links it into the
    neighbouring record, we write ffff. strict is for our records, where one x,y is one node;
    the factory keeps a flyover's two nodes at one x,y on separate rings."""
    bad = []
    by_xy = {}
    for off, (si, k) in nodes.items():
        by_xy.setdefault(strings[si]['pts'][k][1:3], []).append(off)
    for off, (si, k) in nodes.items():
        pt = strings[si]['pts'][k]; key = pt[5]['key']
        if key == 0xffff:
            if len(by_xy[pt[1:3]]) > 1:
                bad.append('node w%d (%d,%d) has key ffff but %d other occurrences' % (off, pt[1], pt[2], len(by_xy[pt[1:3]]) - 1))
            continue
        seen = [off]; cur = key + base
        for _ in range(10):
            if cur not in nodes:
                if not on_edge(pt): bad.append('node w%d key %04x -> w%d is not a node record' % (off, key, cur))
                break
            si2, k2 = nodes[cur]; pt2 = strings[si2]['pts'][k2]
            if pt2[1:3] != pt[1:3]:
                if not on_edge(pt): bad.append('node w%d (%d,%d) key -> w%d (%d,%d): different point' % (off, pt[1], pt[2], cur, pt2[1], pt2[2]))
                break
            if cur == off: break
            seen.append(cur); cur = pt2[5]['key'] + base
        else:
            bad.append('node w%d: ring does not close in 10 hops' % off)
        if strict and (not bad or not bad[-1].startswith('node w%d' % off)):
            if set(seen) != set(by_xy[pt[1:3]]):
                bad.append('node w%d (%d,%d): ring has %d of %d occurrences' % (off, pt[1], pt[2], len(seen), len(by_xy[pt[1:3]])))
    return bad


def check_pair(fa_plain, sec4_plain, sidecar_subs=None, strict=True):
    """Problems in one parcel: the road record alone, then its section-4 record if any."""
    strings = fa_encode.link_strings(fa_plain)
    nodes = nodes_of(strings)
    base = key_base(fa_plain)
    bad = ring_problems(strings, nodes, base, strict)
    g_nodes = {off: strings[si]['pts'][k][5]['G'] for off, (si, k) in nodes.items() if strings[si]['pts'][k][5]['G'] is not None}
    if sec4_plain is None:
        return bad + ['node w%d carries G=%d but the parcel has no section-4 record' % (o, g) for o, g in g_nodes.items()]
    rec = sec4_write.decode(sec4_plain)
    w = struct.unpack('>%dH' % (len(sec4_plain) // 2), sec4_plain)
    if (w[0], w[1]) != struct.unpack_from('>HH', fa_plain, 0): bad.append('parcel codes differ')
    if w[3] != (rec['toff'] if rec['toff'] != 0xffff else rec['nwords']) - 9: bad.append('w3 %d is not text offset - 9' % w[3])
    Ls = sec4_write.sub_offsets(rec['subs'])
    L = sec4_write.FIRST_L
    for n, s in enumerate(rec['subs']):
        if L != Ls[n]: bad.append('sub %d at L=%d, layout says %d' % (n, L, Ls[n]))
        L = w[L] if w[L] != 0xffff else None
        if L is None and n + 1 < len(rec['subs']): bad.append('chain ends after sub %d of %d' % (n + 1, len(rec['subs'])))
    keys = [s['key'] for s in rec['subs'] if 'groups' in s or 'ext' in s]
    if keys != sorted(keys): bad.append('sub-record keys not in increasing order: %s' % keys)
    signed = {}
    for n, s in enumerate(rec['subs']):
        if 'groups' not in s and 'ext' not in s: continue
        off = s['key'] + base
        if off not in nodes: bad.append('sub L=%d key %04x -> w%d is not a node record' % (Ls[n], s['key'], off)); continue
        si, k = nodes[off]; nd = strings[si]['pts'][k][5]
        if nd['G'] != Ls[n]: bad.append('sub L=%d key -> w%d whose G is %s' % (Ls[n], off, nd['G']))
        signed[off] = Ls[n]
        J = strings[si]['pts'][k][1:3]
        for g in s.get('groups', ()):
            for e in g['entries']:
                if e['dir'] & 0x0c00 not in (sec4_write.APPROACH_FWD, sec4_write.APPROACH_BWD):
                    bad.append('sub L=%d dir %04x has no approach bits' % (Ls[n], e['dir']))
                if len(e['pairs']) != 1: continue   # a multi-hop approach may leave the parcel; we write one hop
                _, ref = e['pairs'][0]
                roff = ref + base
                if roff not in nodes: bad.append('sub L=%d ref %04x -> w%d is not a node record' % (Ls[n], ref, roff)); continue
                ai, ka = nodes[roff]; a = strings[ai]
                kj = [i for i, pt in enumerate(a['pts']) if pt[1:3] == J]
                if not kj: bad.append('sub L=%d ref -> w%d: string w%d does not pass the node' % (Ls[n], roff, a['at'])); continue
                kj = kj[0]
                if kj == ka: bad.append('sub L=%d ref -> w%d is the node itself' % (Ls[n], roff)); continue
                lo, hi = sorted((ka, kj))
                if any(a['pts'][i][5] is not None for i in range(lo + 1, hi)):
                    bad.append('sub L=%d ref -> w%d: another node record between it and the node' % (Ls[n], roff))
                want = sec4_write.APPROACH_FWD if ka < kj else sec4_write.APPROACH_BWD
                if e['dir'] & 0x0c00 != want and e['dir'] & 0x0c00 in (sec4_write.APPROACH_FWD, sec4_write.APPROACH_BWD):
                    bad.append('sub L=%d dir %04x disagrees with the ref side' % (Ls[n], e['dir']))
                tb = sec4_write.text_bytes(e['text'])
                if not tb or tb.rstrip(b'\0')[:2] not in (b'0=', b'2=', b'4='): bad.append('sub L=%d text %r' % (Ls[n], e['text'][:20]))
    for off, G in g_nodes.items():
        if signed.get(off) != G: bad.append('node w%d G=%d names no sub-record with that key' % (off, G))
    if sidecar_subs is not None:
        want = sec4_write.encode(dict(code=(w[0], w[1]), country=w[8], subs=sidecar_subs))
        if want != sec4_plain: bad.append('record differs from the sidecar')
    return bad


def check_tile(path, sidecar=None, verbose=False, strict=True):
    """(parcels checked, section-4 records, [problem strings]) over a whole tile."""
    signs = {}
    if sidecar:
        _, per = sec4_write.load_signs(sidecar)
        signs = {(y * 32 + r, x * 32 + c): subs for (y, x), m in per.items() for (r, c), subs in m.items()}
    probs = []; n4 = 0; npar = 0
    with TileScan(path) as t:
        fa = {}
        for r in t.records(section=1, level=1, even_only=True):
            if not r.is_zlib: continue
            d = zlib.decompressobj(); head = d.decompress(r.raw(), 6)
            if struct.unpack_from('>H', head, 4)[0] != 0x00fa: continue
            fa[struct.unpack_from('>HH', head, 0)] = r
        s4 = {}
        for r in t.records(section=4):
            if r.n > 65536 or not r.is_zlib: continue
            try: p = r.plain()
            except zlib.error: continue
            if len(p) < 24 or struct.unpack_from('>H', p, 4)[0] != 9: continue
            s4[struct.unpack_from('>HH', p, 0)] = p
        for code, p in s4.items():
            if code not in fa: probs.append('%04x %04x: section-4 record for a parcel with no 0x00fa record' % code)
        for code, r in fa.items():
            npar += 1
            p4 = s4.get(code)
            if p4 is not None: n4 += 1
            row, col = decode_code(code)
            try:
                bad = check_pair(r.plain(), p4, signs.get((row, col)) if signs else None, strict)
            except (ValueError, IndexError, zlib.error) as e:
                bad = ['not checked: %s' % e]
            probs += ['%04x %04x: %s' % (code + (b,)) for b in bad]
            if verbose and p4 is not None: print('%04x %04x: %d sub-records, %d problems' % (code + (len(sec4_write.decode(p4)['subs']), len(bad))))
        for key in signs:
            if sec4_write.parcel_code(*key) not in s4: probs.append('sidecar parcel %s has no section-4 record' % (key,))
    return npar, n4, probs


def decode_code(code):
    """(row, col) back from a level-1 parcel code, the inverse of sec4_write.parcel_code."""
    w0, w1 = code
    row = ((w0 >> 8) & 0x7f) * 64 + ((w0 >> 4) & 0xf) * 4 + (w0 & 3)
    col = (w1 >> 8) * 64 + ((w1 >> 4) & 0xf) * 4 + (w1 & 3)
    return row, col


if __name__ == '__main__':
    argv = sys.argv[1:]
    side = None
    if '--sidecar' in argv:
        i = argv.index('--sidecar'); side = argv[i + 1]; del argv[i:i + 2]
    verbose = '-v' in argv
    if verbose: argv.remove('-v')
    strict = '--factory' not in argv
    if not strict: argv.remove('--factory')
    npar, n4, probs = check_tile(argv[0], side, verbose, strict)
    print('%s: %d road parcels, %d with a section-4 record, %d problems' % (argv[0].split('/')[-1], npar, n4, len(probs)))
    for p in probs[:40]: print('  ' + p)
    sys.exit(1 if probs else 0)
