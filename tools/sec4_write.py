#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add a section 4 carrying a country code to one of our own tiles.

Section 4 is the per-parcel administrative index. Factory tiles carry it at level 1
only: one directory of kind 0x1046 per mesh, slot = parcel row, pair index = parcel
column, at most one record per parcel. Measured on B68R058R.DAT (Tromso) and
B50R060R.DAT; see REPORT-section4-build.md section 1.

The record is the factory's 56-byte layout: the parcel code, the country word, and the
format constants that are identical across countries (w3 0x0013, the ffff0000 offset
pairs, the 0x0011 and 0x0900 tail words). The last word (bytes 54-55) varies per parcel
in factory data and is undecoded; it is written 0, as tile_reauthor.py does, rather
than the Norwegian parcel's 0x0132.

    sec4_write.py in.DAT out.DAT --country 352 [--meshes y,x,y,x ...]

Only our own tiles. Do not point this at a factory tile: it calls
clarion_tile.read_tile, which is banned there.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile

TEMPLATE = bytes.fromhex(
    '00000000'          # w0,w1 parcel code, filled per record
    '0009'              # w2 kind
    '0013'              # w3 undecoded, the same in every factory record
    'ffff0000ffff0000'  # w4..w7 offset pairs, absent
    '0000'              # w8 country, filled per record
    '00000000ffff0011'
    'ffff0000ffff0000ffff0000ffff0000ffff0000'
    '00000000000009000000')   # last word undecoded and per-parcel in factory data

DIR_KIND, DIR_TYPE, SLOTS, PAIRS = 0x1046, 0x93, 32, 32
ABSENT = struct.pack('>II', 0x7fffffff, 0x7fffffff)   # factory fill, not ffffffff


def parcel_code(row, col):
    """Level-1 parcel code, as clarion_bg.parcel_code writes it for a background record."""
    w0 = 0x8000 | (row >> 6) << 8 | ((row & 63) >> 2) << 4 | 0xc | (row & 3)
    w1 = ((col >> 6) & 0xff) << 8 | ((col & 63) >> 2) << 4 | 0xc | (col & 3)
    return w0, w1


def record(row, col, country):
    p = bytearray(TEMPLATE)
    struct.pack_into('>HH', p, 0, *parcel_code(row, col))
    struct.pack_into('>H', p, 16, country)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(bytes(p)) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    return bytes(p), comp


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


def make_dir(y, x, rows, country):
    """One level-1 section-4 directory for mesh (y, x)."""
    D = dict(off=0, kind=DIR_KIND, ab=(SLOTS, PAIRS), y=y, x=x, typ=DIR_TYPE,
             box=(y + 1, y, x, x + 1), body=256, slots={}, nslots=SLOTS,
             empty_bodies={i: ABSENT * PAIRS for i in range(SLOTS)})
    for slot in sorted(rows):
        body = bytearray(ABSENT * PAIRS)
        recs = []
        for col in rows[slot]:
            plain, comp = record(y * 32 + slot, x * 32 + col, country)
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
    src, dest = argv[0], argv[1]

    t = clarion_tile.read_tile(open(src, 'rb').read())
    assert not any(s['id'] == 4 for s in t['sections']), 'tile already has a section 4'
    rows = road_parcels(t)
    if only is not None:
        rows = {m: v for m, v in rows.items() if m in only}
    assert rows, 'no road parcels selected'
    dirs = [make_dir(y, x, rows[(y, x)], country) for y, x in sorted(rows)]
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
    print('%s -> %s  %d dirs, %d records, country %d, box %s, %d -> %d bytes'
          % (src.split('/')[-1], dest.split('/')[-1], len(dirs), nrec, country,
             box, len(open(src, 'rb').read()), len(out)))


if __name__ == '__main__':
    main(sys.argv[1:])
