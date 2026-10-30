#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add a section 4 to one of our own tiles: the country code per parcel, and with
--signposts the route-guidance sub-records rd_build.py prepared.

Section 4 (type 0x93) is the per-parcel route guidance index. Factory tiles carry it at
level 1 only: one directory of kind 0x1046 per mesh, slot = parcel row, pair index = parcel
column, at most one record per parcel. Measured on B68R058R.DAT (Tromso) and B50R060R.DAT;
see REPORT-section4-build.md section 1. The record layout is SECTION4-draft.md: a header,
a chain of sub-records, then a text block. A parcel without signposts gets the factory's
56-byte form, one empty sub-record carrying only the country.

    sec4_write.py in.DAT out.DAT --country 352 [--meshes y,x,y,x ...] [--signposts side.json]

side.json is what `rd_build.py --signposts side.json` writes: per parcel the sub-records,
whose node keys and link refs are word offsets into that parcel's 0x00fa record, and whose
link-word indices the road record already carries as G words. The layout here must give
every sub-record the link word rd_build computed; sub_offsets() is the one place both
sides get it from.

Only our own tiles. Do not point this at a factory tile: it calls clarion_tile.read_tile,
which is banned there.
"""
import json, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile

TEMPLATE = bytes.fromhex(
    '00000000'          # w0,w1 parcel code, filled per record
    '0009'              # w2 kind
    '0013'              # w3 words from w9 to the text block (none here): 28 - 9
    'ffff0000ffff0000'  # w4..w7 offset pairs, absent
    '0000'              # w8 country, filled per record
    '00000000ffff0011'
    'ffff0000ffff0000ffff0000ffff0000ffff0000'
    '00000000000009000000')   # the empty sub-record's node key, 0 here

DIR_KIND, DIR_TYPE, SLOTS, PAIRS = 0x1046, 0x93, 32, 32
ABSENT = struct.pack('>II', 0x7fffffff, 0x7fffffff)   # factory fill, not ffffffff
FIRST_L = 11          # the first sub-record's link word
GROUP_HDR = 0x14      # group header high byte: one of 14 near-equal values on B48R060R (1,669-2,122 each); meaning unsettled
FLAG = 0x0140         # the (flag, ref) flag of a one-link approach: 6,692 of 6,692 single-pair entries
APPROACH_FWD = 0x0800  # dir bits 11-10: the approach travels the link string in storage order (ref before the node)
APPROACH_BWD = 0x0400  # against storage order (ref after the node); 13,284 of 13,286 factory entries fit
EXIT_CODES = (0x2000, 0x1000, 0x3000, 0x4000, 0x5000)   # bits 14-12: one per exit of an approach, order not reproduced


def parcel_code(row, col):
    """Level-1 parcel code, as clarion_bg.parcel_code writes it for a background record."""
    w0 = 0x8000 | (row >> 6) << 8 | ((row & 63) >> 2) << 4 | 0xc | (row & 3)
    w1 = ((col >> 6) & 0xff) << 8 | ((col & 63) >> 2) << 4 | 0xc | (col & 3)
    return w0, w1


def text_bytes(text):
    """UTF-8, NUL-padded to a word; attr carries this padded length."""
    b = text if isinstance(text, bytes) else text.encode('utf-8')
    return b + b'\0' * (len(b) % 2)


def sub_size(sub):
    """Words of one sub-record including its link word."""
    if 'groups' in sub:
        return 17 + ('ext' in sub) + sum(2 + sum(4 + 1 + 2 * len(e['pairs']) for e in g['entries']) for g in sub['groups'])
    return 18 if 'ext' in sub else 17


def sub_offsets(subs):
    """Link word L of each sub-record, in the order given; the first is FIRST_L."""
    out = []; L = FIRST_L
    for s in subs or [{}]:
        out.append(L); L += sub_size(s)
    return out


def encode(rec):
    """Record bytes from a decoded or prepared record: dict(code, country, subs, ext=[words]).

    A sub-record is dict(key, groups=[dict(hdr, entries=[dict(dir, pairs=[(flag, ref)], text,
    attr_hi=0x4000)])]) for a signpost, dict(key, ext=E) for the extended type, or dict(key)
    for the empty type; a signpost sub-record with ext=E too has E after its key (190 of
    30,943 on B48R060R). rec['ext'] holds the raw extended records between the chain and the
    text block, addressed by the E words. Texts are shared by value, in first-appearance order."""
    subs = rec.get('subs') or [dict(key=0)]
    Ls = sub_offsets(subs)
    texts = {}; block = b''
    for s in subs:
        for g in s.get('groups', ()):
            for e in g['entries']:
                b = text_bytes(e['text'])
                if b not in texts: texts[b] = len(block) // 2; block += b
    body = []
    for n, s in enumerate(subs):
        nxt = Ls[n + 1] if n + 1 < len(subs) else 0xffff
        w = [nxt, sub_size(s)]
        if 'groups' in s:
            w += [0xffff, 0, 0xffff, 0, 16, len(s['groups']), 0xffff, 0, 0xffff, 0, 0x4000 if 'ext' in s else 0, 0, 0, 0]
            for g in s['groups']:
                w += [(g.get('hdr', GROUP_HDR) << 8) | len(g['entries']), 0]
                for e in g['entries']:
                    b = text_bytes(e['text'])
                    w += [e.get('attr_hi', 0x4000) | len(b), 1 + 2 * len(e['pairs']), 0, e['dir']]
                    for flag, ref in e['pairs']: w += [flag, ref]
                    w.append(texts[b])
            w.append(s['key'])
            if 'ext' in s: w.append(s['ext'])
        elif 'ext' in s:
            w += [0xffff, 0] * 5 + [0x4000, 0, 0, 0, s['key'], s['ext']]
        else:
            w += [0xffff, 0] * 5 + [0, 0, 0, 0x0900, s['key']]
        assert len(w) == sub_size(s)
        body += w
    body += list(rec.get('ext', ()))
    toff = FIRST_L + len(body)
    hdr = [rec['code'][0], rec['code'][1], 0x0009, toff - 9, 0xffff, 0,
           toff if block else 0xffff, len(block) // 2, rec['country'], 0, 0]
    return struct.pack('>%dH' % (11 + len(body)), *hdr, *body) + block


def decode(plain):
    """The record as encode() takes it, plus toff and nwords. Text per entry is the padded bytes."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    assert w[2] == 0x0009, 'not a section-4 record: kind %#x' % w[2]
    toff, tlen = w[6], w[7]
    block = plain[2 * toff:2 * (toff + tlen)] if toff != 0xffff else b''
    subs = []; L = FIRST_L
    while True:
        nxt, size = w[L], w[L + 1]
        pairs = [(w[L + 2 + 2 * i], w[L + 3 + 2 * i]) for i in range(5)]
        s = dict(key=w[L + size - 1])
        if pairs[2][0] != 0xffff:
            assert pairs[2][0] == 16, 'pair-3 offset %d at L=%d' % (pairs[2][0], L)
            if w[L + 12] & 0x4000: s['key'], s['ext'] = w[L + size - 2], w[L + size - 1]
            p = L + 16; s['groups'] = []
            for _ in range(pairs[2][1]):
                g = dict(hdr=w[p] >> 8, entries=[]); n = w[p] & 0xff; p += 2
                for _ in range(n):
                    attr, N, _, d = w[p:p + 4]; p += 4
                    k = (N - 1) // 2
                    e = dict(dir=d, pairs=[(w[p + 2 * i], w[p + 1 + 2 * i]) for i in range(k)])
                    to = w[p + 2 * k]; p += N
                    e['text'] = block[2 * to:2 * to + (attr & 0x3fff)]
                    if attr & 0xc000 != 0x4000: e['attr_hi'] = attr & 0xc000
                    g['entries'].append(e)
                s['groups'].append(g)
        elif w[L + 12] & 0x4000:
            s['key'] = w[L + 16]; s['ext'] = w[L + 17]
        subs.append(s)
        if nxt == 0xffff: break
        L = nxt
    end = L + size
    ext_end = toff if toff != 0xffff else len(w)
    return dict(code=(w[0], w[1]), country=w[8], subs=subs, ext=list(w[end:ext_end]), toff=toff, nwords=len(w))


def sign_text(dest, upper=False):
    """KIWI name syntax from OSM destination tags: `0=PLACE\\x1fPLACE;2=REF;4=EXIT`, at most
    six names per type, or None when the tags name nothing. destination:ref:to, the road
    the exit leads to, goes in parentheses as the factory writes a via motorway."""
    def names(*keys):
        out = []
        for k in keys:
            for v in (dest.get(k) or '').split(';'):
                v = v.strip()
                if v and v not in out: out.append(v)
        return out[:6]
    places = names('destination', 'destination:street')
    refs = names('destination:ref') + ['(%s)' % r for r in names('destination:ref:to')]
    exits = names('junction:ref')
    parts = [(0, places), (2, refs[:6]), (4, exits)]
    text = ';'.join('%d=%s' % (t, '\x1f'.join(v)) for t, v in parts if v)
    if not text: return None
    return text.upper() if upper else text


def record(row, col, country, subs=None):
    """(plain, compressed) for one parcel; without subs the 56-byte country-only form."""
    p = encode(dict(code=parcel_code(row, col), country=country, subs=subs))
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(p) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    return p, comp


def road_parcels(t):
    """{(mesh y, mesh x): {slot: [column, ...]}} for section 2 level 1."""
    out = {}
    sec2 = [s for s in t['sections'] if s['id'] == 2][0]
    for L in sec2['levels']:
        if L['level'] != 1:
            continue
        for D in L['dirs']:
            rows = out.setdefault((D['y'], D['x']), {})
            for slot, s in D['slots'].items():
                rows.setdefault(slot, []).extend(r['k'] for r in s['recs'])
    return {m: {s: sorted(set(c)) for s, c in rows.items() if c} for m, rows in out.items()}


def make_dir(y, x, rows, country, signs=None):
    """One level-1 section-4 directory for mesh (y, x); signs = {(row, col): subs}."""
    D = dict(off=0, kind=DIR_KIND, ab=(SLOTS, PAIRS), y=y, x=x, typ=DIR_TYPE,
             box=(y + 1, y, x, x + 1), body=256, slots={}, nslots=SLOTS,
             empty_bodies={i: ABSENT * PAIRS for i in range(SLOTS)})
    for slot in sorted(rows):
        body = bytearray(ABSENT * PAIRS)
        recs = []
        for col in rows[slot]:
            plain, comp = record(y * 32 + slot, x * 32 + col, country, (signs or {}).get((slot, col)))
            struct.pack_into('>II', body, 8 * col, len(comp) // 2, len(plain) // 2)
            recs.append(dict(k=col, raw=comp, words=len(plain) // 2))
        D['slots'][slot] = dict(tag=0, body=bytes(body), recs=recs)
    return D


def bbox_minutes(parcels):
    """Arc-minute box tight around the parcels written, as rd_build.py does for section 2.

    A level-1 parcel is 1.25' by 1.875'; done in integers so the edges land exactly where
    the factory's do. Checked against B68R058R.DAT's own section-4 box: rows 3339..3344,
    columns 30169..30175 give (4182, 4173, 1126, 1140), which is what the tile carries.
    """
    rows = [r for r, c in parcels]
    cols = [c for r, c in parcels]
    y0 = min(rows) * 5 // 4
    y1 = -(-(max(rows) + 1) * 5 // 4)
    x0 = min(cols) * 15 // 8 - 55440
    x1 = -(-(max(cols) + 1) * 15 // 8) - 55440
    return y1, y0, x0, x1


def patch_header(hdr, ndirs, box):
    """Append a section-4 group to the level table at 0x1ac and the box table at 0x2ac."""
    hdr = bytearray(hdr)
    n = struct.unpack_from('>H', hdr, 0x1ac)[0]
    p = 0x1ae
    for _ in range(n):
        p += 2 + 4 * hdr[p + 1]
    assert p + 6 <= 0x2ac, 'level table would run into the box table'
    assert hdr[p:p + 6] == b'\xff' * 6, 'level table slack is not free: %s' % hdr[p:p + 6].hex()
    hdr[p:p + 6] = struct.pack('>BBBBH', 4, 1, 1, DIR_TYPE, ndirs)
    struct.pack_into('>H', hdr, 0x1ac, n + 1)

    n = struct.unpack_from('>H', hdr, 0x2ac)[0]
    p = 0x2ae
    for _ in range(n):
        p += 2 + 10 * hdr[p + 1]
    assert hdr[p:p + 12] == b'\xff' * 12, 'box table slack is not free: %s' % hdr[p:p + 12].hex()
    hdr[p:p + 12] = struct.pack('>BBBB4h', 4, 1, 1, 0, *box)
    struct.pack_into('>H', hdr, 0x2ac, n + 1)

    # the section table at 0x800 gains one entry; write_tile fills it, so only the
    # terminator has to move out of the way
    p = 0x800
    while struct.unpack_from('>I', hdr, p)[0] != 0xffffffff:
        p += 6
    assert p + 12 <= 0x1000
    struct.pack_into('>IH', hdr, p + 6, 0xffffffff, 0)
    return bytes(hdr)


def load_signs(path):
    """{(mesh y, mesh x): {(row, col): subs}} from rd_build's sidecar, entry texts as bytes."""
    side = json.load(open(path))
    out = {}
    for P in side['parcels']:
        subs = P['subs']
        for s in subs:
            for g in s.get('groups', ()):
                g['entries'] = [e for e in g['entries'] if e['text'] is not None]   # a blank sign names nothing
                for e in g['entries']:
                    e['pairs'] = [tuple(p) for p in e['pairs']]
                    e['text'] = e['text'].encode('utf-8')
        out.setdefault((P['y'], P['x']), {})[(P['row'], P['col'])] = subs
    return side, out


def main(argv):
    country = 352
    if '--country' in argv:
        i = argv.index('--country'); country = int(argv[i + 1]); del argv[i:i + 2]
    only = None
    if '--meshes' in argv:
        i = argv.index('--meshes')
        v = [int(x) for x in argv[i + 1].split(',')]
        only = {(v[j], v[j + 1]) for j in range(0, len(v), 2)}
        del argv[i:i + 2]
    signs = {}; nsigns = 0
    if '--signposts' in argv:
        i = argv.index('--signposts'); side, signs = load_signs(argv[i + 1]); del argv[i:i + 2]
        if side.get('country') not in (None, country):
            raise SystemExit('sidecar country %s, tile country %d' % (side.get('country'), country))
        nsigns = sum(len(s) for m in signs.values() for s in m.values())
    src, dest = argv[0], argv[1]

    t = clarion_tile.read_tile(open(src, 'rb').read())
    assert not any(s['id'] == 4 for s in t['sections']), 'tile already has a section 4'
    rows = road_parcels(t)
    if only is not None:
        rows = {m: v for m, v in rows.items() if m in only}
    assert rows, 'no road parcels selected'
    for m, per in signs.items():
        for (slot, col) in per:
            assert m in rows and col in rows[m].get(slot, ()), 'signposts for a parcel without roads: %s %s' % (m, (slot, col))
    dirs = [make_dir(y, x, rows[(y, x)], country, signs.get((y, x))) for y, x in sorted(rows)]
    nrec = sum(len(s['recs']) for D in dirs for s in D['slots'].values())
    covered = {(y * 32 + slot, x * 32 + col)
               for (y, x), r in rows.items() for slot, cols in r.items() for col in cols}
    box = bbox_minutes(covered)

    sec = dict(id=4, count=0, taddr=0,
               levels=[dict(level=1, typ=DIR_TYPE, dirs=dirs)], table_words=0)
    nw, _, _ = clarion_tile.table_words(sec)
    sec['count'] = -(-2 * nw // 256)
    t['sections'].append(sec)
    t['header'] = patch_header(t['header'], len(dirs), box)
    out = clarion_tile.write_tile(t, clarion_tile.default_order(t))
    open(dest, 'wb').write(out)
    print('%s -> %s  %d dirs, %d records, %d signpost sub-records, country %d, box %s, %d -> %d bytes'
          % (src.split('/')[-1], dest.split('/')[-1], len(dirs), nrec, nsigns, country,
             box, len(open(src, 'rb').read()), len(out)))


if __name__ == '__main__':
    main(sys.argv[1:])
