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

Nothing here writes, and none of it has been checked by a round trip yet.
"""
import collections, os, struct, sys, zlib

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
    if len(argv) < 2 or argv[0] not in ('summary', 'keys', 'blob'):
        raise SystemExit(__doc__)
    if argv[0] == 'summary':
        cmd_summary(argv[1:])
    elif argv[0] == 'keys':
        cmd_keys(argv[1:])
    else:
        cmd_blob(argv[1], int(argv[2]) if len(argv) > 2 else 256)


if __name__ == '__main__':
    main(sys.argv[1:])
