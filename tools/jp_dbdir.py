#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the DBDIR map database of a Japanese-market Clarion card. Read-only.

Japanese cards (measured on `GJPNX-10-101-100`, database 15-02, card label 485-1972-00,
in a 2015 Nissan LEAF) carry no MAPAL001 grid. Their map lives in `DBDIR/` as a paged
relational database (Hitachi's Entier, going by the firmware strings), one B/D/I file
triplet per table: B for blobs, D for rows, I for the index. docs/JP-DBDIR.md has the
measurements and the plan.

    jp_dbdir.py summary FILE...     header fields and page census
    jp_dbdir.py keys FILE...        bounding box of the JIS mesh keys in a D file
    jp_dbdir.py blob FILE [PAGES]   framing, zlib and geometry-group census of a B file
    jp_dbdir.py geojson DBDIR TABLE A B
                                    one BPCL or BTPCLnn parcel as WGS84 GeoJSON (A, B hex)

Nothing here writes, and none of it has been checked by a round trip yet.
"""
import collections, json, math, os, struct, sys, zlib

CONTROL = (b'flhb', b'ratm', b'raim', b'sbmb', b'sgmb')
ROW_PAGE = 0x1000          # rows sit in 4 KiB pages even where the header says 0x8000
GROUP = b'\x00\x01\x00\x03'


# --- container -----------------------------------------------------------------------

class Header:
    """The flhb page at offset 0. Little-endian.

    0x00 u16 file id (BPCLB 0x034d, BPCLD 0x034c), 0x02 `01 55`, 0x04 'flhb', 0x08 u16 name length,
    0x0a name, 0x2c u32 (unknown, per file), 0x30 kind ('B' blob, 'U' table or index,
    'S'/'M'/'W' on the ED* user files), 0xde u16 page or segment size, 0xe0 u32 (1, 8 or
    16), 0xe4 build stamp 'YYYY/MM/DD-hh:mm:ss'.
    """

    def __init__(self, b):
        if b[4:8] != b'flhb':
            raise ValueError('not an flhb file')
        n = struct.unpack_from('<H', b, 8)[0]
        self.file_id = struct.unpack_from('<H', b, 0)[0]
        self.name = b[10:10 + n].decode('ascii')
        self.f2c = struct.unpack_from('<I', b, 0x2c)[0]
        self.kind = chr(b[0x30])
        self.page = struct.unpack_from('<H', b, 0xde)[0] or ROW_PAGE
        self.fe0 = struct.unpack_from('<I', b, 0xe0)[0]
        self.stamp = b[0xe4:0xf8].split(b'\0')[0].decode('ascii', 'replace')


def read_header(path):
    with open(path, 'rb') as f:
        return Header(f.read(0x100))


def table_id(path):
    """The u16 that opens every data page, next to `01 55`. It is the same on all data
    pages of a file and shared by a table's B and D files (BPCLB and BPCLD both 0x02f9,
    BTPCL13B and BTPCL13D both 0x0448), so it names the table. It is not a checksum:
    it does not change with the page contents."""
    size = read_header(path).page
    for _, pg in pages(path, size):
        if pg[4:8] not in CONTROL and pg[2:4] == b'\x01\x55' and any(pg[4:8]):
            return struct.unpack_from('<H', pg, 0)[0]
    return None


def pages(path, size, limit=None):
    """(offset, bytes) for every page of `size`, control pages included."""
    with open(path, 'rb') as f:
        o = 0
        while limit is None or o < limit * size:
            b = f.read(size)
            if len(b) < size:
                return
            yield o, b
            o += size


# --- rows ----------------------------------------------------------------------------

def page_rows(pg):
    """(offset in page, flags, row) for each row of a data page.

    A row is `u16 length, u16 flags` and its body. Rows run from offset 4 to the first
    length that does not fit; the rest of the page is free space.
    """
    if pg[4:8] in CONTROL:
        return
    o = 4
    while o + 4 <= len(pg):
        n, fl = struct.unpack_from('<HH', pg, o)
        if n < 6 or o + n > len(pg):
            return
        yield o, fl, pg[o:o + n]
        o += n


def columns(row):
    """Split a table row into (presence bitmap, column bytes).

    Flags 0x4001 and 0x0001: `u8 ncols` then ceil(ncols/8) bitmap bytes, most significant
    bit first, one bit per column present. Flags 0x0000 (the PADRN address tables): the
    columns follow the header directly and there is no bitmap.
    """
    fl = struct.unpack_from('<H', row, 2)[0]
    if fl in (0x4001, 0x0001):
        nc = row[4]
        nb = (nc + 7) // 8
        bits = int.from_bytes(row[5:5 + nb], 'big')
        present = [bool(bits >> (8 * nb - 1 - i) & 1) for i in range(nc)]
        return present, row[5 + nb:]
    if fl == 0:
        return None, row[4:]
    raise ValueError('not a table row: flags %#06x' % fl)


def table_rows(path, limit=None):
    for _, pg in pages(path, ROW_PAGE, limit):
        for _, fl, row in page_rows(pg):
            if fl in (0x4001, 0x0001, 0x0000):
                yield row


# --- JIS mesh keys -------------------------------------------------------------------

def mesh_key(lon, lat):
    """(A, B) for a point: A = (lon - 100) * 128, B = lat * 192, both floored.

    That is JIS X 0410: A >> 7 and B >> 7 are the two halves of the first-mesh code, the
    next three bits the second mesh and the low four a sixteenth of it, a cell 28.125" of
    longitude by 18.75" of latitude. Both are u16 on the card, so the encoding has no room
    for a latitude south of the equator or a longitude west of 100 E.
    """
    a = int((lon - 100.0) * 128)
    b = int(lat * 192)
    if not (0 <= a < 0x10000 and 0 <= b < 0x10000):
        raise ValueError('outside the JIS mesh range: %r, %r' % (lon, lat))
    return a, b


def mesh_point(a, b):
    """South-west corner of cell (A, B) as (lon, lat)."""
    return 100.0 + a / 128.0, b / 192.0


def first_mesh(a, b):
    """The four-digit first-mesh code, e.g. 5339 for central Tokyo."""
    return (b >> 7) * 100 + (a >> 7)


def key_of(row, skip=0):
    """The (A, B) key a map table row opens with, u16 little-endian, after `skip` bytes."""
    _, body = columns(row)
    return struct.unpack_from('<HH', body, skip)


# --- blobs ---------------------------------------------------------------------------

def blob_payloads(path, limit=None):
    """Payload bytes of the B-file rows (flags low byte 0x09), zlib streams inflated.

    Payloads are big-endian. Page-filling rows (0x4009) continue on the next page and
    are yielded unjoined, so a zlib stream that crosses a page fails to inflate and is
    skipped.
    """
    size = read_header(path).page
    for _, pg in pages(path, size, limit):
        for _, fl, row in page_rows(pg):
            if fl & 0xff != 0x09:
                break
            b = row[4:]
            if b[:2] == b'\x78\x9c':
                try:
                    b = zlib.decompress(b)
                except zlib.error:
                    continue
            yield b


def section_table(b):
    """The (offset, length) table a drawing record opens with, in 16-bit words, or None.

    `u16 table length`, then `(u32 offset, u16 length)` per section, `ffffffff 0000`
    for an absent one. The first section starts where the table ends and the rest
    follow it without gaps. Two shapes seen: 58 words (19 sections) and 43 (14).
    """
    if len(b) < 8:
        return None
    n = struct.unpack_from('>H', b, 0)[0]
    if not 4 <= 2 * n <= len(b) or (2 * n - 2) % 6:
        return None
    out = []
    for o in range(2, 2 * n, 6):
        off, ln = struct.unpack_from('>IH', b, o)
        out.append(None if off == 0xffffffff else (off, ln))
    used = [s for s in out if s]
    if not used or used[0][0] != n:
        return None
    return out


def group_headers(b):
    """Counter of geometry-group kinds in a payload: `0001 0003 kind count`, word-aligned.

    The same group header the X08 tiles use (GEOMETRY.md); the shapes after it are not.
    """
    c = collections.Counter()
    i = 0
    while True:
        i = b.find(GROUP, i)
        if i < 0 or i + 6 > len(b):
            return c
        if i % 2 == 0 and b[i + 4] in (0x80, 0x40, 0x00):
            c[{0x80: 'polygon', 0x40: 'polyline', 0x00: 'point'}[b[i + 4]]] += 1
        i += 2


# --- drawing parcels (BPCL, BTPCLnn) ------------------------------------------------
#
# A D row holds the parcel key (A, B), a u16, then blob references; each reference names
# a row of the B file. The drawing record is a section table, each section a list of
# geometry groups. Decoded on card 485-1972-00: every BPCL and BTPCL polygon closes
# (1.5 M and 49 M), and BPCL level-1 sea outlines land on OSM coastline within metres.

def blob_refs(b, limit=None):
    """Blob references in a D row after the key and column 1.

    Tag 0x29, 0x2a or 0x2b gives the width (1, 2 or 3 bytes, LE) of the length that
    follows, then a locator: u8 slot (1-based row in the page) and u24 LE page (1-based,
    in units of the B file's page size). With `limit`, stop after that many references
    and return (refs, the rest of the row): BTPCL rows carry two u32 columns after them.
    """
    out = []
    o = 0
    while o < len(b) and (limit is None or len(out) < limit):
        w = b[o] - 0x28
        if not 1 <= w <= 3:
            raise ValueError('not a blob reference at +%d: %02x' % (o, b[o]))
        out.append((int.from_bytes(b[o + 1:o + 1 + w], 'little'), b[o + 1 + w],
                    int.from_bytes(b[o + 2 + w:o + 5 + w], 'little')))
        o += 5 + w
    return out if limit is None else (out, b[o:])


def read_blob(f, ref, page=0x8000):
    """The payload a (length, slot, page) reference names, zlib streams inflated.

    A row with flags 0x4009 continues in another row: its first four bytes are the
    locator (u8 slot, u24 LE page) of the next piece.
    """
    ln, slot, pno = ref
    parts = []
    for _ in range(256):
        f.seek((pno - 1) * page)
        pg = f.read(page)
        o = 4
        for _ in range(slot - 1):
            o += struct.unpack_from('<H', pg, o)[0]
        n, fl = struct.unpack_from('<HH', pg, o)
        body = pg[o + 4:o + n]
        if fl != 0x4009:
            parts.append(body)
            break
        slot, pno = body[0], int.from_bytes(body[1:4], 'little')
        parts.append(body[4:])
    b = b''.join(parts)
    if len(b) != ln:
        raise ValueError('blob holds %d bytes, its reference says %d' % (len(b), ln))
    if b[:2] == b'\x78\x9c':
        b = zlib.decompress(b)
    return b


def record_sections(b):
    """{index: tuple of BE words} for the non-empty sections of a drawing record."""
    w = struct.unpack('>%dH' % (len(b) // 2), b[:len(b) // 2 * 2])
    out = {}
    for i, o in enumerate(range(1, w[0], 3)):
        off = w[o] << 16 | w[o + 1]
        if off != 0xffffffff:
            out[i] = w[off:off + w[o + 2]]
    return out


Shape = collections.namedtuple('Shape', 'kind id scales type attr pts breaks tail')


def _s8(v):
    return v - 0x100 if v & 0x80 else v


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def shapes(sec):
    """The shapes of one section, in parcel units 0..4096 (x east, y north).

    Section: `u16 groups, u16 2*groups+1, u16 kind|count`, then for each further group
    `u16 word offset from the section start, u16 kind|count`. kind|count: 0x8000
    polygon, 0x4000 polyline, 0 point, low 14 bits the number of shapes.
    Shape: `u16 flags|length` (low 13 bits the length in words, 0x2000 = trailing words
    after the points), `u16 id`, `u16 scales<<13 | n`, `u16 type`, `u16 attr`, then x, y
    and, for lines and polygons, n deltas: int8 (dx, dy) in one word, or two int16 words
    when attr & 0x0100. A zero delta adds no point; it toggles whether the outline is
    drawn (see drawn_runs).
    """
    ng, hl = sec[0], sec[1]
    if hl != 2 * ng + 1:
        raise ValueError('bad group header %04x %04x' % (ng, hl))
    heads = [(None, sec[2])] + [(sec[3 + 2 * i], sec[4 + 2 * i]) for i in range(ng - 1)]
    q = hl
    for goff, kc in heads:
        if goff is not None and goff != q:
            raise ValueError('group at word %d, expected %d' % (goff, q))
        kind = kc >> 8 & 0xc0
        for _ in range(kc & 0x3fff):
            L = sec[q] & 0x1fff
            if L < 7 or q + L > len(sec):
                raise ValueError('bad shape length %#06x at word %d' % (sec[q], q))
            w = sec[q:q + L]
            n = w[2] & 0x1fff
            wide = bool(w[4] & 0x0100)
            need = 7 if kind == 0 else 7 + (2 * n if wide else n)
            if need > L or (need < L and not sec[q] & 0x2000):
                raise ValueError('shape %d: %d deltas do not fill %d words' % (w[1], n, L))
            x, y = w[5], w[6]
            pts, breaks = [(x, y)], []
            for i in range(n if kind else 0):
                if wide:
                    dx, dy = _s16(w[7 + 2 * i]), _s16(w[8 + 2 * i])
                else:
                    dx, dy = _s8(w[7 + i] >> 8), _s8(w[7 + i] & 0xff)
                if dx == dy == 0:
                    breaks.append(len(pts))
                    continue
                x += dx
                y += dy
                pts.append((x, y))
            yield Shape(kind, w[1], w[2] >> 13, w[3], w[4], pts, breaks, w[need:])
            q += L
    if q != len(sec):
        raise ValueError('%d words left over' % (len(sec) - q))


def drawn_runs(s):
    """The drawn parts of a polygon outline, as point lists.

    The outline starts drawn and each zero delta toggles it. Undrawn edges are the parcel
    border and the straight cuts that split large polygons, so the drawn runs of a sea
    polygon are the coastline.
    """
    runs, cur = [], []
    for j in range(1, len(s.pts)):
        if sum(1 for k in s.breaks if k <= j) % 2 == 0:
            cur = cur or [s.pts[j - 1]]
            cur.append(s.pts[j])
        elif cur:
            runs.append(cur)
            cur = []
    return runs + [cur] if cur else runs


EXTENT = 4096


def bpcl_level(a, b):
    """BPCL key level: level 1 keys end in 0xc|n on both halves (a quarter of a second
    mesh, n = 0..3); the others carry the level in A's low nibble and 0xf in B's."""
    return 1 if a & 0xc == 0xc and b & 0xc == 0xc else a & 0xf


def bpcl_parcel(a, b):
    """(lon, lat) of the SW corner and (width, height), all in 1/8 arc-second, of a BPCL
    level 1 (1.875' x 1.25') or level 2 (one second mesh, 7.5' x 5') parcel. The corner
    is also stored in the parcel's column-2 blob, as BE u32 lat and lon in 1/2048"."""
    lv = bpcl_level(a, b)
    lon = (100 + (a >> 7)) * 28800 + ((a >> 4) & 7) * 3600
    lat = ((b >> 7) * 2400 + ((b >> 4) & 7) * 300) * 8
    if lv == 1:
        return (lon + (a & 3) * 900, lat + (b & 3) * 600), (900, 600)
    if lv == 2:
        return (lon, lat), (3600, 2400)
    raise ValueError('level %d parcel' % lv)


def btpcl_parcel(a, b):
    """The same for a BTPCLnn key: the plain JIS cell, 1/16 of a second mesh."""
    return ((100 * 128 + a) * 225, b * 150), (225, 150)


def parcel_point(x, y, corner, size):
    """Parcel units -> (lon, lat) degrees on the Tokyo Datum."""
    return ((corner[0] + x * size[0] / EXTENT) / 28800.0,
            (corner[1] + y * size[1] / EXTENT) / 28800.0)


def tokyo_to_wgs84(lon, lat):
    """Tokyo Datum (Bessel 1841) -> WGS84 by the three-parameter shift (-146.414,
    507.337, 680.507) m. The map data fits OSM best this way, remote islands included."""
    def ecef(lon, lat, a, f):
        e2 = f * (2 - f)
        la, lo = math.radians(lat), math.radians(lon)
        n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
        return n * math.cos(la) * math.cos(lo), n * math.cos(la) * math.sin(lo), n * (1 - e2) * math.sin(la)
    x, y, z = ecef(lon, lat, 6377397.155, 1 / 299.152813)
    x, y, z = x - 146.414, y + 507.337, z + 680.507
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    p = math.hypot(x, y)
    la = math.atan2(z, p * (1 - e2))
    for _ in range(6):
        la = math.atan2(z + e2 * a / math.sqrt(1 - e2 * math.sin(la) ** 2) * math.sin(la), p)
    return math.degrees(math.atan2(y, x)), math.degrees(la)


def parcel_geojson(dbdir, table, a, b):
    """Every shape of one parcel's drawing record as WGS84 GeoJSON features."""
    btpcl = table.startswith('BTPCL')
    corner, size = (btpcl_parcel if btpcl else bpcl_parcel)(a, b)
    col = 4 if btpcl else 5
    page = read_header(os.path.join(dbdir, table + 'B')).page
    for row in table_rows(os.path.join(dbdir, table + 'D')):
        present, body = columns(row)
        if struct.unpack_from('<HH', body) != (a, b):
            continue
        cols = [i for i, p in enumerate(present) if p][2:]
        if btpcl:
            cols = [c for c in cols if c <= 5]
        refs, _ = blob_refs(body[6:], len(cols))
        refs = dict(zip(cols, refs))
        if col not in refs:
            return []
        with open(os.path.join(dbdir, table + 'B'), 'rb') as f:
            rec = read_blob(f, refs[col], page)
        feats = []
        ll = lambda q: list(tokyo_to_wgs84(*parcel_point(q[0], q[1], corner, size)))
        for i, sec in sorted(record_sections(rec).items()):
            try:
                got = list(shapes(sec))
            except ValueError:
                continue
            for s in got:
                prop = {'section': i, 'type': '%04x' % s.type, 'attr': '%04x' % s.attr, 'id': s.id}
                if s.kind == 0x80:
                    geom = {'type': 'Polygon', 'coordinates': [[ll(q) for q in s.pts]]}
                elif s.kind == 0x40:
                    geom = {'type': 'LineString', 'coordinates': [ll(q) for q in s.pts]}
                else:
                    geom = {'type': 'Point', 'coordinates': ll(s.pts[0])}
                feats.append({'type': 'Feature', 'properties': prop, 'geometry': geom})
        return feats
    return []


# --- command line --------------------------------------------------------------------

def cmd_summary(paths):
    for p in paths:
        h = read_header(p)
        census = collections.Counter()
        for _, pg in pages(p, h.page, 2048):
            census[pg[4:8].decode('ascii') if pg[4:8] in CONTROL else 'data'] += 1
        t = table_id(p)
        print('%s  %s  kind %s  page %#x  file id %#06x  table id %s  %s  %s' % (
            os.path.basename(p), h.name, h.kind, h.page, h.file_id,
            '%#06x' % t if t is not None else '-', h.stamp, dict(census)))


def cmd_keys(paths, skip=0):
    for p in paths:
        n = ok = 0
        lo = [0xffff, 0xffff]
        hi = [0, 0]
        for row in table_rows(p):
            n += 1
            try:
                a, b = key_of(row, skip)
            except (ValueError, struct.error):
                continue
            lon, lat = mesh_point(a, b)
            if 120 <= lon <= 160 and 18 <= lat <= 50:
                ok += 1
                lo = [min(lo[0], a), min(lo[1], b)]
                hi = [max(hi[0], a), max(hi[1], b)]
        if not ok:
            print('%s  %d rows, none keyed' % (os.path.basename(p), n))
            continue
        (x0, y0), (x1, y1) = mesh_point(*lo), mesh_point(*hi)
        print('%s  %d rows, %.1f%% keyed  lon %.3f..%.3f  lat %.3f..%.3f  meshes %04d..%04d' % (
            os.path.basename(p), n, 100.0 * ok / n, x0, x1, y0, y1, first_mesh(*lo), first_mesh(*hi)))


def cmd_blob(path, limit):
    st = collections.Counter()
    kinds = collections.Counter()
    for b in blob_payloads(path, limit):
        st['payloads'] += 1
        t = section_table(b)
        if t:
            st['tables of %d' % len(t)] += 1
            kinds.update(group_headers(b))
    print(os.path.basename(path), dict(st), dict(kinds))


def main(argv):
    if len(argv) < 2 or argv[0] not in ('summary', 'keys', 'blob', 'geojson'):
        raise SystemExit(__doc__)
    if argv[0] == 'geojson':
        feats = parcel_geojson(argv[1], argv[2], int(argv[3], 16), int(argv[4], 16))
        json.dump({'type': 'FeatureCollection', 'features': feats}, sys.stdout)
        print()
    elif argv[0] == 'summary':
        cmd_summary(argv[1:])
    elif argv[0] == 'keys':
        cmd_keys(argv[1:])
    else:
        cmd_blob(argv[1], int(argv[2]) if len(argv) > 2 else 256)


if __name__ == '__main__':
    main(sys.argv[1:])
