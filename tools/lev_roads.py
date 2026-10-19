#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Coarse roads in LEV3456R.DAT: levels 3 and 4.

Two jobs. First the section-2 level-4 and level-5 road directories are moved west, as the
backgrounds were: their box still said 904..992 after lev_coarse ran. Then road-shape
records (kind 0x00fa, the even pair next to the background record) go into the level-4 and
level-3 parcels. No graph records: both levels draw without them, and a graph is routing
data that nothing on this card reads at those levels.

    ./lev_roads.py --profile iceland IN.DAT OUT.DAT [roads.json]
    --classes motorway,trunk,primary   overrides the profile's coarse set
    --simplify METRES                  level-4 tolerance; level 3 uses a quarter of it

That in-place pass edits an existing coarse file, appending what it rewrites. The
clean-room coarse file is written whole by lev_build.py, which calls build() below for
its road shapes; that is the path the generated cards use.

The coarse road shape, as build() writes it and check() verifies it on stock files:
Road shapes for the coarse map file: levels 3, 4 and 5 of LEV3456R.DAT.

Level 3 draws at 2 and 4 km, level 4 at 10 and 16 km, level 5 at 64 km (docs/LEVEL5.md).
Levels 3 and 4 keep road shapes in their own record, kind 0x00fa in the even pair beside the
background record; level 5 keeps them as a road block inside the background record. All
three use the level-2 record layout: eleven layer pairs, roads of classes 3 to 7 in layer 6
and 0 to 2 in layer 7. Labels are route numbers only, `2=REF` in the background record's
names section, one sub-table item per shape: every coarse label on the 2015 card reads that
way (3,065 at level 5, 13,231 at level 4, 64,902 at level 3).

A shape: length in words (0x2000 / 0x4000 add one / two header words), 0x8000 | points
(level 3: points alone), class << 12, word 3, the name entry's word offset or 0xffff, 0, 0,
then the start point and the links. Levels 4 and 5 open every link with its id; level 3
carries no ids. x flags, as at level 2 without the style word:

    0x2000  first point of a link (levels 4 and 5; level 3 has none)
    0x1000  the link goes on past this point, or the shape's last point
    0x0800  this point does not end a link, and the start point

A link end inside the shape clears 0x1000 and 0x0800, and at levels 4 and 5 carries 0x0800
on its y, which says a link header follows. The start's y has 0x0800 at levels 4 and 5.
encode(decode(words)) gives the factory words back for every shape decode() accepts:
check() runs it over a stock coarse file.

    ./lev_roads.py --check STOCK_LEV3456R.DAT
"""
import collections, json, math, os, sys, struct, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lev_coarse, clarion_bg, clarion_profile, rd_build
from clarion_tile import addr_of, enc_addr, pad256

L4_TAIL = 0x0318         # per-parcel token seen in a shipped level-4 pair; any value, same in both
L3_TAIL = 0x02d4
PW, PH = 2.0, 4 / 3      # level-4 parcel in degrees (2x2 meshes)
PW3, PH3 = 0.5, 1 / 3    # level-3 parcel in degrees (30' x 20')

# South-west corner of the database grid, in degrees and in meshes. Set from a profile.
LON0, LAT0 = -20.0, 32 / 1.5
Y0, X0 = 32, 904

CLASS_CODE = {'motorway': 3, 'motorway_link': 3, 'trunk': 4, 'trunk_link': 4,
              'primary': 5, 'primary_link': 5, 'secondary': 6, 'secondary_link': 6}
CLASS_CODE3 = dict(CLASS_CODE, tertiary=7, tertiary_link=7)


def use_profile(P):
    global LON0, LAT0, Y0, X0
    Y0, X0 = P.mesh_origin
    LON0, LAT0 = clarion_bg.mesh_to_lon(X0), clarion_bg.mesh_to_lat(Y0)


def dir_of_section2(d, slot):
    a2 = addr_of(struct.unpack_from('>I', d, 0x806)[0]); nw = struct.unpack_from('>H', d, a2)[0]
    w = list(struct.unpack_from('>%dH' % nw, d, a2)); o = w[slot]
    return a2, nw, w, o


def shift_section2(d, slot, ncol, meshes):
    """Move a whole-database road directory ncol columns (meshes in all) west; rebuilt at
    the end of the file."""
    a2, nw, w, o = dir_of_section2(d, slot)
    y1, y0, x0, x1, z, t = w[o+2:o+8]; a = addr_of((z << 16) | t)
    size2, cols, rows, hy, hx, typ = struct.unpack_from('>6H', d, a); body = cols * 8
    slots = []; p = a + 12
    for r in range(rows):
        tag = struct.unpack_from('>I', d, p)[0]
        pairs = list(struct.unpack_from('>%dI' % (body // 4), d, p + 4)); recs = [None] * cols
        if tag != 0xffffffff:
            q = addr_of(tag)
            for k in range(cols):
                ln, X = pairs[2*k], pairs[2*k+1]
                if ln == 0xffffffff: continue
                n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X); recs[k] = bytes(d[q:q+n]); q += n
        pairs = [0xffffffff, 0] * ncol + pairs[:-2 * ncol]; recs = [None] * ncol + recs[:-ncol]
        slots.append((tag, pairs, recs)); p += 4 + body
    nx0, nx1 = x0 - meshes, x1 - meshes
    pos0 = pad256(len(d))
    out = bytearray(struct.pack('>6H', size2, cols, rows, hy, (nx0 - 1024) & 0xffff, typ))
    dsize = 12 + rows * (4 + body); blocks = bytearray(); bpos = pad256(pos0 + dsize)
    for tag, pairs, recs in slots:
        if tag == 0xffffffff:
            out += struct.pack('>I', tag) + struct.pack('>%dI' % (body // 4), *pairs); continue
        block = b''.join(r for r in recs if r is not None)
        out += struct.pack('>I', enc_addr(bpos + len(blocks))) + struct.pack('>%dI' % (body // 4), *pairs)
        blocks += block; blocks += b'\0' * (pad256(len(blocks)) - len(blocks))
    out += b'\0' * (bpos - pos0 - len(out)); out += blocks
    d += b'\0' * (pos0 - len(d)); d += out
    a = enc_addr(pos0); w[o+4], w[o+5], w[o+6], w[o+7] = nx0, nx1, a >> 16, a & 0xffff
    struct.pack_into('>%dH' % nw, d, a2, *w)
    print(f'section 2 slot {slot}: {rows}x{cols} moved to x {nx0}..{nx1}, directory at {pos0:#x}')


def shape_words_l4(pts, cls):
    """The unnamed shipped form: class in the top nibble of word 2, word 3 0x3000; start
    point, then per link an id and its end point (middle 0x2000|x, 0x4800|y; last
    0x3000|x, 0x4000|y)."""
    body = [0x8000 | len(pts), cls << 12, 0x3000, 0xffff, 0, 0, 0x0800 | pts[0][0], 0x4800 | pts[0][1]]
    for i, (x, y) in enumerate(pts[1:], start=1):
        last = i == len(pts) - 1
        body += [i, (0x3000 if last else 0x2000) | x, (0x4000 if last else 0x4800) | y]
    return [len(body) + 1] + body


def shape_words_l3(pts, cls):
    """Level-3 road shape: 0x4000|points, class, 0, no name, 0, 0, then the points
    (start 0x0800|x)."""
    body = [0x4000 | len(pts), cls << 12, 0, 0xffff, 0, 0]
    for i, (x, y) in enumerate(pts): body += [(0x0800 if i == 0 else 0x1000) | x, y]
    return [len(body) + 1] + body


def fa_record(level, code, shapes, tail):
    """A road-shape record with its shapes in layer 6, where every shipped coarse record
    of classes 3 to 6 keeps them. Layer 5 is empty throughout the shipped data."""
    unit = [0xffff, 0x0000] * 11
    unit[12], unit[13] = 11, len(shapes)          # layer 6, data at dword 11
    for s in shapes: unit += s
    if len(unit) % 2: unit.append(0)
    w = [code[0], code[1], 0x00fa, 0xffff, 0xffff, 0, 0, 11, len(unit) // 2,
         0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0, tail, 0]
    plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(plain) + c.flush()
    if len(comp) % 2: comp += b'\0'
    return plain, comp


def parcel4(lon, lat):
    return (int((lat - LAT0) // PH), int((lon - LON0) // PW))


def quant4(lon, lat, row, col):
    lon0, lat0 = LON0 + col * PW, LAT0 + row * PH
    return (max(0, min(2047, round((lon - lon0) / PW * 2048))),
            max(0, min(2047, round((lat - lat0) / PH * 2048))))


def parcel3(lon, lat):
    """(cell y0, cell x0, row, col) of a level-3 parcel: 16 rows by 16 columns to a cell."""
    r = int((lat - LAT0) // PH3); c = int((lon - LON0) // PW3)
    return (Y0 + 8 * (r // 16), X0 + 8 * (c // 16), r % 16, c % 16)


def quant3(lon, lat, pc):
    lon0, lat0, w, h = clarion_bg.parcel_geo(3, *pc)
    return (max(0, min(2047, round((lon - lon0) / w * 2048))),
            max(0, min(2047, round((lat - lat0) / h * 2048))))


def runs(ways, PW, PH, parcel, quant, maxpts=200):
    """Per parcel, the polyline (in parcel units) of each way's run inside it."""
    parcels = {}
    for wy in ways:
        pts = wy['nodes']; cur = None; run = []
        def flush():
            if cur is not None and len(run) > 1:
                for i in range(0, len(run) - 1, maxpts - 1):
                    piece = run[i:i + maxpts]
                    if len(piece) > 1: parcels.setdefault(cur, []).append((piece, wy['hw']))
        for a, b in zip(pts, pts[1:]):
            seg = [a]; cr = []
            for k in range(1, 50):
                lon = (math.floor((min(a[0], b[0]) - LON0) / PW) + k) * PW + LON0
                if lon >= max(a[0], b[0]): break
                if b[0] != a[0]: cr.append((lon - a[0]) / (b[0] - a[0]))
            for k in range(1, 50):
                lat = (math.floor((min(a[1], b[1]) - LAT0) / PH) + k) * PH + LAT0
                if lat >= max(a[1], b[1]): break
                if b[1] != a[1]: cr.append((lat - a[1]) / (b[1] - a[1]))
            for t in sorted(c for c in cr if 0 < c < 1):
                seg.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            seg.append(b)
            for p, q in zip(seg, seg[1:]):
                pc = parcel((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                if pc != cur:
                    flush(); cur = pc; run = [quant(*p, pc)]
                e = quant(*q, pc)
                if e != run[-1]: run.append(e)
        flush()
    return parcels


def add_level3(d, parcels):
    """Even-pair shape records into the level-3 cell directories. Each cell that is touched
    is rewritten at the end of the file and its section-1 entry re-pointed."""
    a1 = addr_of(struct.unpack_from('>I', d, 0x800)[0]); nw = struct.unpack_from('>H', d, a1)[0]
    w = list(struct.unpack_from('>%dH' % nw, d, a1))
    o = w[4]; cnt = w[o + 1]; entries = {}
    for i in range(cnt):
        y1, y0, x0, x1 = w[o+2+6*i:o+6+6*i]; entries[(y0, x0)] = i
    by_cell = {}
    for (y0, x0, row, col), segs in parcels.items():
        by_cell.setdefault((y0, x0), {}).setdefault(row, {})[col] = segs
    added = 0
    for cell, rows in sorted(by_cell.items()):
        if cell not in entries: continue
        i = entries[cell]; z, t = w[o+6+6*i], w[o+7+6*i]; a = addr_of((z << 16) | t)
        hdr = d[a:a+12]; body = 256; slots = []; p = a + 12
        for r in range(16):
            tag = struct.unpack_from('>I', d, p)[0]
            pairs = list(struct.unpack_from('>64I', d, p + 4)); recs = [None] * 32
            if tag != 0xffffffff:
                q = addr_of(tag)
                for k in range(32):
                    ln, X = pairs[2*k], pairs[2*k+1]
                    if ln in (0xffffffff, 0x7fffffff): continue
                    n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X); recs[k] = bytes(d[q:q+n]); q += n
            slots.append([tag, pairs, recs, False]); p += 4 + body
        for row, cols in rows.items():
            for col, segs in cols.items():
                if slots[row][2][2 * col + 1] is None: continue   # no background record here
                shapes = [shape_words_l3(pts, c) for pts, c in segs]
                code = clarion_bg.parcel_code(3, 2 * cell[0] + row, 2 * cell[1] + col)
                plain, comp = fa_record(3, code, shapes, L3_TAIL)
                slots[row][2][2 * col] = comp
                slots[row][1][4 * col], slots[row][1][4 * col + 1] = len(comp) // 2, len(plain) // 2
                slots[row][3] = True; added += 1
        pos0 = pad256(len(d)); out = bytearray(hdr); dsize = 12 + 16 * (4 + body)
        blocks = bytearray(); bpos = pad256(pos0 + dsize)
        for tag, pairs, recs, dirty in slots:
            if dirty:
                block = b''.join(r for r in recs if r is not None)
                tag = enc_addr(bpos + len(blocks))
                blocks += block; blocks += b'\0' * (pad256(len(blocks)) - len(blocks))
            out += struct.pack('>I', tag) + struct.pack('>64I', *pairs)
        out += b'\0' * (bpos - pos0 - len(out)); out += blocks
        d += b'\0' * (pos0 - len(d)); d += out
        na = enc_addr(pos0); w[o+6+6*i], w[o+7+6*i] = na >> 16, na & 0xffff
    struct.pack_into('>%dH' % nw, d, a1, *w)
    return added



# ---- the clean-room coarse roads: lev_build.py --roads ----

GRAMMAR_Y = 0x2800       # y bits the grammar sets: header follows (0x0800), style follows (0x2000)


def header_len(w0):
    return 7 + (1 if w0 & 0x2000 else 0) + (2 if w0 & 0x4000 else 0)


def decode(words, level):
    """A shape as dict(head, links=[(id or None, [(x, y, ybits)])]); ybits are y's bits
    outside the grammar, kept as found. None if it does not follow the grammar."""
    L = words[0] & 0x1fff
    if L != len(words): return None
    h = header_len(words[0])
    head = list(words[1:h])
    npts = words[1] & (0x3fff if level != 3 else 0x3fff)
    q = h
    sx, sy = words[q], words[q + 1]; q += 2
    if sx & 0xf800 != 0x0800: return None
    ids = level != 3
    if ids and sy & GRAMMAR_Y != 0x0800: return None
    if not ids and sy & GRAMMAR_Y: return None
    start = (sx & 0x7ff, sy & 0x7ff, sy & ~(0x7ff | GRAMMAR_Y) & 0xffff)
    links = []; prev_y = sy
    while q < L:
        lid = None
        if ids:
            if not prev_y & 0x0800 or prev_y & 0x2000: return None
            lid = words[q]; q += 1
        pts = []
        while q + 1 < L + 1:
            if q + 1 >= L: return None
            x, y = words[q], words[q + 1]; q += 2
            first = not pts
            last = q >= L
            end = not x & 0x1000
            pts.append((x & 0x7ff, y & 0x7ff, y & ~(0x7ff | GRAMMAR_Y) & 0xffff))
            prev_y = y
            if end or last: break
        links.append((lid, pts))
        if q >= L: break
    s = dict(head=head, start=start, links=links)
    return s if encode(s, level) == list(words) else None


def encode(s, level):
    """Words of a shape from decode()'s form."""
    ids = level != 3
    sx, sy, sb = s['start']
    out = list(s['head']) + [0x0800 | sx, sy | sb | (0x0800 if ids else 0)]
    n = len(s['links'])
    for i, (lid, pts) in enumerate(s['links']):
        if ids: out.append(lid)
        for k, (x, y, yb) in enumerate(pts):
            link_end = k == len(pts) - 1; chain_end = i == n - 1
            xf = (0x2000 if k == 0 and ids else 0) | (0 if link_end and not chain_end else 0x1000) \
                 | (0 if link_end else 0x0800)
            yf = 0x0800 if link_end and not chain_end and ids else 0
            out += [xf | x, y | yb | yf]
    hl = 1 + len(s['head'])
    L = len(out) + 1
    w0 = L | (0x2000 if hl == 8 else 0x4000 if hl == 9 else 0x6000 if hl == 10 else 0)
    return [w0] + out


def shape(level, cls, links, name_off=0xffff):
    """A shape from points: links is [[(x, y), ...], ...], the first point of the first link
    being the shape's start, each later link starting where the previous ended (that point
    not repeated). Named shapes carry word 3 0 and plain y words; unnamed ones word 3 0x3000
    and 0x4000 on every y, the two commonest factory forms at levels 3 and 4."""
    named = name_off != 0xffff
    yb = 0 if named else 0x4000
    start = links[0][0]
    body = [[(x, y, yb) for x, y in links[0][1:]]] + [[(x, y, yb) for x, y in l] for l in links[1:]]
    npts = 1 + sum(len(b) for b in body)
    head = [(0x8000 if level != 3 else 0) | npts, cls << 12, 0 if named else 0x3000, name_off, 0, 0]
    ids = list(range(1, len(body) + 1)) if level != 3 else [None] * len(body)
    return encode(dict(head=head, start=(start[0], start[1], yb), links=list(zip(ids, body))), level)


def shapes_of(w, g):
    """(layer, words) for every shape of a level-2 layout unit starting at word g."""
    out = []
    for li in range(11):
        off, cnt = w[g + 2 * li], w[g + 1 + 2 * li]
        if off == 0xffff: continue
        q = g + 2 * off
        for _ in range(cnt):
            L = w[q] & 0x1fff
            out.append((li, w[q:q + L])); q += L
    return out


def factory_shapes(lev):
    """(level, layer, words) for every road shape of a coarse file read by lev_file.read."""
    for L in lev['sections'][0]['levels']:
        if L['level'] not in (3, 4, 5): continue
        for D in L['dirs']:
            for row in D['rows'] or []:
                if not row: continue
                for k, r in enumerate(row):
                    if r is None or 'plain' not in r: continue
                    p = r['plain']
                    if len(p) < 44: continue
                    w = struct.unpack('>%dH' % (len(p) // 2), p)
                    if w[2] != 0x00fa: continue
                    if L['level'] == 5:
                        if w[7] in (0, 0xffff): continue
                    elif D['pairs'] != 2 * D['cols'] or k % 2: continue
                    for li, s in shapes_of(w, 2 * w[7]):
                        yield L['level'], li, list(s)


# OSM highway -> coarse class is CLASS_CODE3 above: the top nibble of shape word 2
LEVEL_CLASSES = {3: {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'motorway_link', 'trunk_link',
                     'primary_link', 'secondary_link', 'tertiary_link'},
                 4: {'motorway', 'trunk', 'primary', 'secondary'},
                 5: {'motorway', 'trunk', 'primary', 'secondary'}}
# Douglas-Peucker in metres per level. The quantum is about 12 x 18 m at level 3, 48 x 72 m
# at 4 and 190 x 290 m at 5 (64 N); factory segments run a median 218, 201 and 290 m.
TOLERANCE = {3: 10.0, 4: 40.0, 5: 150.0}
MAXPTS = 60
LAYER = 6                 # classes 3 to 7 at every coarse level; 0 to 2 would go in 7
DIR_MESHES = {3: 8, 4: 88, 5: 88}
GRID = {3: 16, 4: 44, 5: 11}


def chains(ways):
    """Join ways that meet end to end and share a class and a route number (or a name) into
    one run, so a route split into many OSM ways draws as a few long shapes."""
    groups = collections.defaultdict(list)
    for w in ways:
        groups[(CLASS_CODE3[w['hw']], w.get('ref') or w.get('name') or ('way', w['id']))].append(w)
    out = []
    for (cls, key), gw in groups.items():
        ends = collections.defaultdict(list)
        for i, w in enumerate(gw):
            for e in (0, -1): ends[tuple(w['nodes'][e])].append(i)
        left = set(range(len(gw)))
        while left:
            i = next((i for i in sorted(left) if any(len(ends[tuple(gw[i]['nodes'][e])]) == 1 for e in (0, -1))),
                     min(left))
            left.discard(i)
            pts = [tuple(p) for p in gw[i]['nodes']]
            for _ in range(2):
                while True:
                    nxt = [j for j in ends.get(pts[-1], []) if j in left]
                    if not nxt: break
                    j = nxt[0]; left.discard(j)
                    seg = [tuple(p) for p in gw[j]['nodes']]
                    if seg[-1] == pts[-1]: seg.reverse()
                    pts += seg[1:]
                pts.reverse()
            out.append(dict(hw=gw[i]['hw'], cls=cls, ref=gw[i].get('ref'), nodes=pts))
    return out


def parcel_runs(pts, level, origin):
    """Per parcel, the polyline's runs inside it in parcel units. A parcel key is (directory
    y0, x0, row, col)."""
    import clarion_bg
    oy, ox = origin
    lon0, lat0, w, h = clarion_bg.parcel_geo(level, oy, ox, 0, 0)
    n = GRID[level]; m = DIR_MESHES[level]
    def key(lon, lat):
        r, c = int((lat - lat0) // h), int((lon - lon0) // w)
        return (oy + m * (r // n), ox + m * (c // n), r % n, c % n)
    def q(p, k):
        x0, y0, pw, ph = clarion_bg.parcel_geo(level, *k)
        return (max(0, min(2047, round((p[0] - x0) / pw * 2048))), max(0, min(2047, round((p[1] - y0) / ph * 2048))))
    out = []; cur = None; run = []
    for a, b in zip(pts, pts[1:]):
        seg = [a]; cr = []
        for step, i, base in ((w, 0, lon0), (h, 1, lat0)):
            if a[i] == b[i]: continue
            k = math.floor((min(a[i], b[i]) - base) / step) + 1
            while base + k * step < max(a[i], b[i]):
                cr.append((base + k * step - a[i]) / (b[i] - a[i])); k += 1
        for t in sorted(c for c in cr if 0 < c < 1):
            seg.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        seg.append(b)
        for p0, p1 in zip(seg, seg[1:]):
            k = key((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
            if k != cur:
                if cur is not None and len(run) > 1: out.append((cur, run))
                cur = k; run = [q(p0, k)]
            e = q(p1, k)
            if e != run[-1]: run.append(e)
    if cur is not None and len(run) > 1: out.append((cur, run))
    return out


def record(code, shapes, country):
    """A level-3 or level-4 road-shape record: the level-2 layout, shapes in layer 6, word
    20 the country (02d4, Spain, on the Canary Islands' records)."""
    unit = [0xffff, 0x0000] * 11
    if shapes: unit[2 * LAYER], unit[2 * LAYER + 1] = 11, len(shapes)
    for sh in shapes: unit += sh
    if len(unit) % 2: unit.append(0)
    w = [code[0], code[1], 0x00fa, 0xffff, 0xffff, 0, 0, 11, len(unit) // 2,
         0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0, country, 0]
    return struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit)


def road_block(plain, shapes, country):
    """A level-5 background record with a road block put in front of its background, which
    moves to w9/w10; names and the rest come through as they were."""
    w = list(struct.unpack_from('>22H', plain, 0))
    assert w[2] == 0x00fa and w[7] == 0xffff and w[9] not in (0, 0xffff), 'not a level-5 background record'
    unit = [0xffff, 0x0000] * 11
    unit[2 * LAYER], unit[2 * LAYER + 1] = 11, len(shapes)
    for sh in shapes: unit += sh
    if len(unit) % 2: unit.append(0)
    road = struct.pack('>%dH' % len(unit), *unit)
    assert w[9] == 11, 'the background must follow the header directly'
    shift = len(road) // 4
    for i in (9, 11, 13, 15, 17):
        if w[i] not in (0, 0xffff): w[i] += shift
    w[7], w[8] = 11, shift
    w[20] = country
    return struct.pack('>22H', *w) + road + plain[44:]


def build(levels, ways, origin, country):
    """Put road shapes into a coarse file's section-1 directories, as lev_build lays them
    out: levels {level: [directory]}, rows of records {'plain': bytes}, background in the
    last pair of each column. Returns {level: (records, shapes, points, labels)}."""
    import fa_encode, rd_build
    stats = {}
    for level in (3, 4, 5):
        sel = [dict(w) for w in ways if w['hw'] in LEVEL_CLASSES[level]]
        runs = chains(sel)
        for r in runs: r['nodes'] = rd_build.simplify([list(p) for p in r['nodes']], TOLERANCE[level])
        per = collections.defaultdict(list)
        for r in runs:
            for k, run in parcel_runs(r['nodes'], level, origin):
                for i in range(0, len(run) - 1, MAXPTS - 1):
                    piece = run[i:i + MAXPTS]
                    if len(piece) > 1: per[k].append((r['cls'], r['ref'], piece))
        dirs = {(D['y'] if level == 3 else origin[0], (D['x'] if level == 3 else origin[1])): D for D in levels[level]}
        nrec = nsh = npt = nlab = 0
        for (y0, x0, row, col), items in sorted(per.items()):
            D = dirs.get((y0, x0))
            if D is None or D['rows'][row] is None: continue
            ppc = D['pairs'] // D['cols']
            bg = D['rows'][row][col * ppc + ppc - 1]
            if bg is None: continue
            labels = list(dict.fromkeys('2=' + ref for _, ref, _ in items if ref))
            _, offs = fa_encode.names_section(labels, [])
            shapes = [shape(level, cls, [piece], offs['2=' + ref] if ref else 0xffff) for cls, ref, piece in items]
            subs = [fa_encode.item_words(sh, 2, sh[4], country) for sh in shapes]
            section, _ = fa_encode.names_section(labels, subs)
            plain = rd_build.merge_names(bg['plain'], section)
            if level == 5:
                D['rows'][row][col] = dict(plain=road_block(plain, shapes, country))
            else:
                code = struct.unpack_from('>2H', plain, 0)
                D['rows'][row][col * ppc + ppc - 1] = dict(plain=plain)
                D['rows'][row][col * ppc] = dict(plain=record(code, shapes, country))
            nrec += 1; nsh += len(shapes); npt += sum(len(p) for _, _, p in items); nlab += len(labels)
        stats[level] = (nrec, nsh, npt, nlab)
    return stats


def check(path):
    import lev_file
    lev = lev_file.read(open(path, 'rb').read())
    n = collections.Counter(); ok = collections.Counter(); forms = collections.Counter()
    for level, layer, words in factory_shapes(lev):
        n[level] += 1
        s = decode(words, level)
        if s: ok[level] += 1; forms[(level, len(s['links']) > 1)] += 1
    for level in sorted(n):
        print('level %d: %d shapes, %d re-encode to the same words (%.2f%%), %d of them multi-link'
              % (level, n[level], ok[level], 100 * ok[level] / n[level], forms[(level, True)]))
    return n, ok


def main():
    argv = sys.argv[1:]
    if argv[:1] == ['--check']:
        for p in argv[1:]: check(p)
        return
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    classes = P.coarse_classes
    tol = P.coarse_simplify_metres
    west = P.west_meshes
    if '--classes' in argv:
        i = argv.index('--classes'); classes = set(argv[i+1].split(',')); del argv[i:i+2]
    if '--simplify' in argv:
        i = argv.index('--simplify'); tol = float(argv[i+1]); del argv[i:i+2]
    if '--west-meshes' in argv:
        i = argv.index('--west-meshes'); west = int(argv[i+1]); del argv[i:i+2]
    src, dest = argv[0], argv[1]
    rpath = argv[2] if len(argv) > 2 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')
    d = bytearray(open(src, 'rb').read())
    if west:
        shift_section2(d, 1, west // 8, west)    # level 5, 8-mesh columns
        shift_section2(d, 2, west // 2, west)    # level 4, 2-mesh columns

    ways = [w for w in json.load(open(rpath)) if w['hw'] in classes]
    for w in ways: w['nodes'] = rd_build.simplify(w['nodes'], tol)
    parcels = runs(ways, PW, PH, parcel4, lambda lon, lat, pc: quant4(lon, lat, *pc))
    D = lev_coarse.read_dir(d, 4)
    assert D['box'][2] == X0, (D['box'], X0)
    added = 0
    for (row, col), rl in sorted(parcels.items()):
        if not (0 <= row < D['rows'] and 0 <= col < D['cols']): continue
        shapes = [shape_words_l4(pts, CLASS_CODE.get(hw, 6)) for pts, hw in rl]
        code = clarion_bg.parcel_code(4, 2 * (Y0 + 2 * row), 2 * (X0 + 2 * col))
        plain, comp = fa_record(4, code, shapes, L4_TAIL)
        s = D['slots'][row]; k = 2 * col
        if s['recs'][k + 1] is None: continue     # no background record here: leave it
        s['recs'][k] = comp
        s['pairs'][2*k], s['pairs'][2*k+1] = len(comp) // 2, len(plain) // 2
        s['dirty'] = True; added += 1
    lev_coarse.write_dir(d, D)
    print(f'level-4 shape records added: {added} parcels, '
          f'{sum(len(v) for v in parcels.values())} segments')

    ways3 = [w for w in json.load(open(rpath)) if w['hw'] in CLASS_CODE3]
    for w in ways3: w['nodes'] = rd_build.simplify(w['nodes'], tol / 4)
    p3 = runs(ways3, PW3, PH3, parcel3, quant3)
    n3 = add_level3(d, {pc: [(pts, CLASS_CODE3.get(hw, 7)) for pts, hw in rl]
                        for pc, rl in p3.items()})
    print(f'level-3 shape records added: {n3}')
    open(dest, 'wb').write(d)
    print(f'wrote {dest} {len(d)} bytes')


if __name__ == '__main__':
    main()
