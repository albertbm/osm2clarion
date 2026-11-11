#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rewrite a map tile with an empty road set: background kept, roads gone.

Kept byte for byte: every background record of section 1 (the odd pair of a parcel column,
land, water, areas and their names), its directories and their parcel boxes, and the header's
database fields. Dropped: the road-shape records of section 1 (the even pairs, with the extra
records of an extended slot), every section 2 record (the road graph) and section 4
(signposts and the country record). Section 2 is left with one empty directory at levels 1
and 2, as rd_build.py writes when it has no roads, since a tile that declares none hangs the
first draw. rd_build.py then puts a real road set into the result.

LEV3456R.DAT, the coarse file, goes through lev_file. At levels 4 and 3 the first pair of a
column is the road record and is dropped, the second is the background and is kept. A level-5
record carries its roads as a block at w7/w8 in front of the background, after a list of the
countries they cross; both are cut out and the background and names move up, unchanged. Level 6 is kept and section 2 (the coarse
graph) is emptied, as lev_build.py writes it without a graph.

The tile is read through tile_scan, never whole, and the format comes from the profile; a
tile in another format is refused.

    road_strip.py --profile NAME SRC OUT [SRC OUT ...]
"""
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_profile
import clarion_tile
import lev_file
import rd_encode
import tile_header
from clarion_bg import tile_cell
from tile_scan import TileScan, addr_of

ROAD_ABSENT = {1: b'\x7f\xff\xff\xff' * 2, 2: b'\xff' * 8}   # level 1 marks an absent road pair 7fffffff
BG_ABSENT = b'\xff' * 8


def boxes(t):
    """{dir offset: (y1, y0, x0, x1)} from the section tables."""
    out = {}
    levels = t._walk(tile_header.LEVEL_OFF, 4)
    p = tile_header.SECT_OFF
    for sid, lv in levels:
        a = struct.unpack_from('>I', t.m, p)[0]; p += 6
        ta = addr_of(a)
        nw = struct.unpack_from('>H', t.m, ta)[0]
        w = struct.unpack_from('>%dH' % nw, t.m, ta)
        for li in range(len(lv)):
            o = w[1 + li]
            if o == 0xffff:
                continue
            for g in range(w[o + 1]):
                y1, y0, x0, x1, hi, lo = w[o + 2 + 6 * g:o + 8 + 6 * g]
                out[addr_of(hi << 16 | lo)] = (y1, y0, x0, x1)
    return out


def background_dirs(t):
    """Section 1 as {level: [directory]} in clarion_tile's form, background records only."""
    box = boxes(t)
    bg = {}
    for r in t.records(section=1):
        if not r.is_road_pair:
            bg.setdefault((r.dir_off, r.slot), []).append(r)
    dirs, nkept = {}, 0
    cur = None
    for sid, lev, doff, slot, p, tag, ext, b in t.slots(section=1):
        assert lev in (1, 2), 'section 1 level %d in a tile' % lev
        if cur is None or cur['off'] != doff:
            _, a, bb, y, x, typ = struct.unpack_from('>6H', t.m, doff)
            body = b - (512 if ext else 0)
            cur = dict(off=doff, kind=(12 + 32 * (4 + body)) // 2, ab=(a, bb), y=y, x=x, typ=typ, box=box[doff],
                       body=body, nslots=32, slots={}, empty_bodies={})
            dirs.setdefault(lev, []).append(cur)
        recs = bg.get((doff, slot), [])
        if not recs:
            # an empty row keeps its stored fill unless it was an extended one
            raw = bytes(t.m[p + 4:p + 4 + cur['body']]) if tag == 0xffffffff and not ext else None
            cur['empty_bodies'][slot] = raw or (ROAD_ABSENT[lev] + BG_ABSENT) * (cur['body'] // 16)
            continue
        if ext:
            body = bytearray((ROAD_ABSENT[lev] + BG_ABSENT) * (cur['body'] // 16))
        else:
            body = bytearray(t.m[p + 4:p + 4 + cur['body']])
            for k in range(0, cur['body'] // 8, 2):
                body[8 * k:8 * k + 8] = ROAD_ABSENT[lev]
        out = []
        for r in recs:
            assert r.sub == 0
            k = 2 * r.col + 1
            body[8 * k:8 * k + 8] = struct.pack('>II', r.n // 2 if r.is_zlib else (r.n // 2 - r.words), r.words)
            out.append(dict(k=k, raw=r.raw(), words=r.words))
        cur['slots'][slot] = dict(tag=tag, body=bytes(body), recs=out)
        nkept += len(out)
    return dirs, nkept


def cut_road_block(plain):
    """A level-5 record without its road block, or the record itself if it has none."""
    w = list(struct.unpack_from('>22H', plain))
    if w[7] == 0xffff:
        return plain
    # units 11 to w7 list further countries after w20; shipped records without roads have none
    assert w[7] >= 11 and w[9] == w[7] + w[8], 'road block not in front of the background: %s' % w[7:13]
    assert all(w[i] == 0xffff for i in (13, 15, 17)), 'an unknown section is in use: %s' % w[13:19]
    end = w[11] + w[12] if w[11] != 0xffff else w[9] + w[10]
    assert w[11] == 0xffff or w[11] == w[9] + w[10], 'names do not follow the background'
    assert 4 * end == len(plain), 'record does not end where its sections do'
    rest = plain[4 * w[9]:]
    w[7], w[8] = 0xffff, 0     # w20, the record's country, stays
    w[9] = 11
    if w[11] != 0xffff:
        w[11] = w[9] + w[10]
    return struct.pack('>22H', *w) + rest


def cut_record(r):
    if r is None or 'plain' not in r:
        return r
    plain = cut_road_block(r['plain'])
    return r if plain is r['plain'] else dict(plain=plain)


def strip_lev(src, out, fmt):
    with open(src, 'rb') as f:
        lev = lev_file.read(f.read())
    if lev['fmt'] is not fmt:
        raise SystemExit('%s is %s, the profile says %s' % (src, lev['fmt'].name, fmt.name))
    n_in = nkept = 0
    for sec in lev['sections']:
        for L in sec['levels']:
            for D in L['dirs']:
                for i, row in enumerate(D['rows']):
                    if not row:
                        continue
                    n_in += sum(r is not None for r in row)
                    if sec['id'] == 2:
                        continue
                    if D['pairs'] == 2 * D['cols']:
                        row = [None if k % 2 == 0 else r for k, r in enumerate(row)]
                    elif L['level'] == 5:
                        row = [cut_record(r) for r in row]
                    D['rows'][i] = row if any(r is not None for r in row) else None
                    nkept += sum(r is not None for r in row)
            if sec['id'] == 2:
                L['dirs'] = []
    fields = dict(lev['fields'])
    fields['box_rows'] = [(sid, r) for sid, r in fields['box_rows'] if sid == 1] + [(2, [])]
    lev['fields'] = fields
    b = lev_file.write(lev)
    with open(out, 'wb') as f:
        f.write(b)
    return 'LEV3456R', n_in, nkept, len(b)


def strip(src, out, fmt):
    if os.path.basename(src).upper().endswith('LEV3456R.DAT'):
        return strip_lev(src, out, fmt)
    m = re.search(r'B[0-9A-F]{2}R[0-9A-F]{3}R', os.path.basename(src).upper())
    assert m, 'no cell name in %s' % src
    cell = m.group(0)
    with TileScan(src) as t:
        if t.fmt is not fmt:
            raise SystemExit('%s is %s, the profile says %s' % (src, t.fmt.name, fmt.name))
        fields, _ = tile_header.fields(bytes(t.m[:0x1000]))
        n_in = sum(1 for _ in t.records())
        dirs, nkept = background_dirs(t)
    sec1 = dict(id=1, levels=[dict(level=lv, typ=ty, dirs=dirs.get(lv, [])) for lv, ty in fmt.levels[1]])
    y0, x0 = tile_cell(cell)
    sec2 = dict(id=2, levels=[dict(level=lv, typ=ty, dirs=[rd_encode.make_dir(lv, y0, x0, {}, fmt)] if lv in (1, 2) else [])
                              for lv, ty in fmt.levels[2]])
    t = dict(sections=[sec1, sec2])
    for sec in t['sections']:
        nw, _, _ = clarion_tile.table_words(sec)
        sec['count'] = -(-2 * nw // 256)
    rows = [(sid, r) for sid, r in fields['box_rows'] if sid == 1] + [(2, [])]
    t['header'] = bytes(tile_header.author(tile_header.level_groups(t, fmt), rows, fields['db_bounds'],
                                           fields['data_version'], fields['source'], fmt=fmt))
    b = clarion_tile.write_tile(t, clarion_tile.default_order(t))
    with open(out, 'wb') as f:
        f.write(b)
    return cell, n_in, nkept, len(b)


if __name__ == '__main__':
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    if not argv or len(argv) % 2:
        sys.exit(__doc__)
    for src, out in zip(argv[::2], argv[1::2]):
        cell, n_in, nkept, size = strip(src, out, P.fmt)
        print('%s %s: %d records in, %d background kept, %d bytes -> %s' % (cell, P.fmt.name, n_in, nkept, size, out))
