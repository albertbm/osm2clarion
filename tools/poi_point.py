#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add facilities from extract_pois.py to a POINT file.

    poi_point.py POINT001.DAT pois.json out.DAT [--fold Þ=TH,þ=th,Ð=D,ð=d] [--adm-lang 20]
                 [--ref-base 0x8000] [--lang 63]

The file is taken apart with point_io.Point and put back with every search frame rebuilt:
the letter trie and data records (one per word of every name, flag 0x4000 on the later
words), the name frame, the ADM frame (slots 280 and 287, the settlements' municipality and
settlement rows) and the per-category frames (slot 15 nearby trees, slot 278 CATEGORY
tries) for every facility category present, the ones already in the file included. With
an empty pois.json the output is the input, byte for byte, which is the check this runs
on the ADM frame before touching anything.

Each facility gets the area ladder of the nearest settlement record, the position words of
its level-1 parcel (x first), the settlements' placeholder refs (--ref-base + 4 * index,
above every value measured on factory files so a wrong read fails rather than resolves),
no address, no telephone and a data id past the highest one in the file. --fold adds
keyboard foldings to addre_io.fold_key for the trie keys; --adm-lang is the language the
ADM rows carry (20 where the country's language is none of the 18 menu languages).
"""
import json
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addre_io, clarion_bg, frames, point_io  # noqa: E402

PARCEL_DEG = (1 / 48, 1 / 32)    # level-1 parcel height and width in degrees


def keyunits(s, kw):
    out = bytearray()
    for ch in s:
        b = ch.encode('utf-8')
        assert len(b) <= kw, s
        out += b + b'\0' * (kw - len(b))
    return bytes(out)


PUNCT = {'\xa0': ' ', '\u2013': ' ', '\u2014': ' ', '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"'}
unfolded = []     # characters no folding reached, dropped from the keys and reported


def word_keys(name, kw, fold=None):
    """One key per word start, the whole remainder from it, and whether it is a later word."""
    s = addre_io.fold_key(''.join(PUNCT.get(c, c) for c in name), fold).upper()
    if not s.isascii():
        unfolded.extend(c for c in s if not c.isascii())
        s = ''.join(c if c.isascii() else ' ' for c in s)
    starts = [0] + [j + 1 for j, c in enumerate(s) if c == ' ' and j + 1 < len(s)]
    return [(keyunits(s[p:], kw), p > 0) for p in starts if s[p].isalnum()]


def parcel_latlon(la, lo, x, y):
    row, col = clarion_bg.parcel_rowcol(1, la, lo)
    return (row + y / 2048) * PARCEL_DEG[0], (col + x / 2048) * PARCEL_DEG[1] - 924


def adm_frame(at, p, fold, adm_lang):
    """The ADM frame for the settlements of a parsed POINT file, at file offset at."""
    names, keys = [], {}
    for nr in p.names:
        f = point_io.parse_name_record(nr)
        if f['category'] != point_io.SETTLEMENT:
            continue
        names += [(f['name'], f['levels'][:2], adm_lang), (f['name'], f['levels'], adm_lang)]
    for i, (nm, lv, lang) in enumerate(names):
        for u, later in word_keys(nm, frames.ADM_KW, fold):
            keys.setdefault(u, []).append((i, later))
    return frames.adm(at, p.area, p.nlevel, names, keys)


def merge(d, pois, fold=None, adm_lang=20, ref_base=0x8000, lang=point_io.LANG_LOCAL):
    """Bytes of the POINT file d with the facilities of pois added."""
    p = point_io.Point(d)
    old_adm, h, dfo = adm_frame(p.extra_off, p, fold, adm_lang)
    if d[p.extra_off:p.extra_off + len(old_adm)] != old_adm:
        raise SystemExit('the ADM frame rebuilt from the settlements differs from the file; '
                         'check --fold and --adm-lang')
    fields = [point_io.parse_name_record(nr) for nr in p.names]
    towns = [(f['levels'], parcel_latlon(f['lat'], f['lon'], f['x'], f['y']))
             for f in fields if f['category'] == point_io.SETTLEMENT]

    def ladder(lat, lon):
        if not towns:
            return (p.area,) + (0,) * (p.nlevel - 1)
        return min(towns, key=lambda t: math.hypot(t[1][0] - lat, (t[1][1] - lon) * math.cos(math.radians(lat))))[0]

    hits = {}        # key -> [(flags, name index, record bytes)]
    for k, start, n in p.keys:
        for i in range(start, start + n):
            w = struct.unpack('>20H', p.rec(i))
            hits.setdefault(k, []).append((w[6], w[15], p.rec(i)))
    next_id = 1 + max([struct.unpack_from('>I', p.rec(i), 32)[0] for i in range(p.nrec)
                       if struct.unpack_from('>I', p.rec(i), 32)[0] != 0xffffffff] + [0])
    names = list(p.names)
    for poi in pois:
        idx = len(names)
        la, lo, y, x = addre_io.parcel_point(poi['lat'], poi['lon'])
        lv = ladder(poi['lat'], poi['lon'])
        ref = ref_base + 4 * idx
        names.append(point_io.name_record(poi['name'], lv, p.nlevel, poi['cat'], poi['sub'], lat=la, lon=lo, x=x, y=y,
                                          refs=((2, 0x40, ref), (3, 0x40, ref + 1))))
        for u, later in word_keys(poi['name'], p.kw, fold):
            fl = 0x4000 if later else 0
            hits.setdefault(u, []).append((fl, idx, point_io.data_record(
                lv, p.nlevel, idx, poi['cat'], poi['sub'], lat=la, lon=lo, x=x, y=y, lang=lang, flags=fl,
                data_id=next_id)))
        next_id += 1
    keys, recs = [], []
    for u in sorted(hits):
        hs = sorted(hits[u], key=lambda h: h[:2])
        keys.append((u, len(recs), len(hs)))
        recs += [h[2] for h in hs]
    # the +0x3c hint stays what the file has: facilities carry two refs and no items, below any settlement's
    out, _nb, _tb = point_io.assemble(p.prefix, p.df, keys, recs, names, list(p.tels), p.cr, p.kw, p.area, p.nlevel)
    at = frames.up(len(out))
    q = point_io.Point(out)
    frame, h, dfo = adm_frame(at, q, fold, adm_lang)
    out = bytearray(out) + b'\0' * (at - len(out)) + frame
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
    # the category frames: every facility category, its trie keys and records in file order
    cat_of = {i: point_io.parse_name_record(nr)['category'] for i, nr in enumerate(names)}
    cats = []
    for code in sorted({c for c in cat_of.values() if c != point_io.SETTLEMENT}):
        ck, cr = [], []
        for u, start, n in keys:
            mine = [r for r in recs[start:start + n] if cat_of[struct.unpack_from('>H', r, 30)[0]] == code]
            if mine:
                ck.append((u, len(cr), len(mine)))
                cr += mine
        cats.append((code, ck, cr))
    if cats:
        at = frames.up(len(out))
        blob, s15, s278 = point_io.category_frames(at, p.area, cats, names)
        out += b'\0' * (at - len(out)) + blob
        struct.pack_into('>IH', out, 0x800 + 6 * 15, *s15)
        struct.pack_into('>IH', out, 0x800 + 6 * 278, *s278)
    return bytes(out), cats


def main(argv):
    def opt(name, default):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default
    fold = opt('--fold', None)
    fold = dict(kv.split('=') for kv in fold.split(',')) if fold else None
    adm_lang = int(opt('--adm-lang', '20'))
    ref_base = int(opt('--ref-base', '0x8000'), 0)
    lang = int(opt('--lang', str(point_io.LANG_LOCAL)))
    src, pois_path, dest = argv
    d = open(src, 'rb').read()
    pois = json.load(open(pois_path))
    out, cats = merge(d, pois, fold, adm_lang, ref_base, lang)
    open(dest, 'wb').write(out)
    p, q = point_io.Point(d), point_io.Point(out)
    print('%s: names %d -> %d, records %d -> %d, %d -> %d bytes; categories %s'
          % (dest, len(p.names), len(q.names), p.nrec, q.nrec, len(d), len(out),
             ' '.join('%d:%d' % (code, len({struct.unpack_from(">H", r, 30)[0] for r in cr})) for code, ck, cr in cats)))
    if unfolded:
        print('  %d characters no folding reached, dropped from the keys: %s' % (len(unfolded), ''.join(sorted(set(unfolded)))))


if __name__ == '__main__':
    main(sys.argv[1:])
