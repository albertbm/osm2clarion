#!/usr/bin/env python3
"""Add one icon point to the building-scale map (HOUSE001) at a given coordinate.

Factory HOUSE001 files carry icon points in layer 10 as `0001 0003 COUNT` then
`TYPE ffff X Y` per point, padded to 8 bytes (measured on stock Everett and Vienna files).
This adds one such point to the parcel holding the given coordinate and rewrites the
file's record blocks, since the record grows. A no-op rewrite reproduces the input byte
for byte.

    house_badge.py IN.DAT OUT.DAT --lon LON --lat LAT --icon 0x806c [--check]

--check: rewrite only, must equal IN.
"""
import argparse
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import house_encode as h
from clarion_tile import enc_addr, pad256

HDR = 24           # header words of the even record
PAIRS = 12         # layer table pairs
ROW = 4 + 32 * 8   # directory row: block tag plus 32 (compressed, plain) word counts


def addr_of(v):
    return (v >> 8) * 2048 + (v & 0xff) * 256


def compress(plain):
    """house_encode.compress without its shapely import: 8 KiB window, as bg_encode."""
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(plain) + c.flush()
    return comp + b'\0' * (len(comp) % 2)


def read_rows(d):
    """{row: {k: plain bytes}} of the one directory, from the slot-14 section table."""
    toff = addr_of(struct.unpack_from('>I', d, 0x800 + 6 * 14)[0])
    doff = addr_of(struct.unpack_from('>10H', d, toff)[9])
    assert struct.unpack_from('>3H', d, doff)[:3] == (0x0826, 16, 16), 'not a HOUSE001 directory'
    rows = {}
    p = doff + 12
    for s in range(16):
        tag = struct.unpack_from('>I', d, p)[0]
        if tag != 0xffffffff:
            q = addr_of(tag)
            for k in range(32):
                ln, x = struct.unpack_from('>II', d, p + 4 + 8 * k)
                if ln in (0xffffffff, 0x7fffffff):
                    continue
                z = d[q:q + 2] == b'\x58\x85'
                n = 2 * ln if z else 2 * (ln + x)
                rows.setdefault(s, {})[k] = zlib.decompress(d[q:q + n]) if z else d[q:q + n]
                q += n
        p += ROW
    return doff, rows


def write_rows(d, doff, rows):
    """The file up to the directory, then the directory and the record blocks rebuilt."""
    out = bytearray(d[:doff + 12])
    blocks = bytearray()
    pos = pad256(doff + 12 + 16 * ROW)
    for s in range(16):
        if s not in rows:
            out += b'\xff' * ROW
            continue
        pairs = [b'\xff' * 8] * 32
        blk = bytearray()
        for k, plain in sorted(rows[s].items()):
            comp = compress(plain)
            pairs[k] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
            blk += comp
        out += struct.pack('>I', enc_addr(pos)) + b''.join(pairs)
        blk += b'\0' * (pad256(len(blk)) - len(blk))
        blocks += blk
        pos += len(blk)
    out += b'\0' * (pad256(len(out)) - len(out))
    return bytes(out + blocks)


def layers_of(plain):
    """(header words, {layer: group bytes}) of an even record."""
    w = struct.unpack_from('>%dH' % HDR, plain, 0)
    g0 = w[7] * 8
    out = {}
    for i in range(PAIRS):
        pos, ln = struct.unpack_from('>HH', plain, g0 + 4 * i)
        if pos != 0xffff and ln:
            out[i] = plain[g0 + pos * 8:g0 + (pos + ln) * 8]
    return w, out


def record_of(w, layers):
    """An even record from its header words and raw groups, laid out as house_encode does."""
    table = [(0xffff, 0)] * (PAIRS - 1) + [(0, 0)]
    pos, groups = PAIRS // 2, b''
    for layer in sorted(layers):
        g = layers[layer]
        assert len(g) % 8 == 0
        table[layer] = (pos, len(g) // 8)
        pos += len(g) // 8
        groups += g
    geo = b''.join(struct.pack('>HH', *p) for p in table) + groups
    w = list(w)
    w[7], w[8] = HDR // 4, len(geo) // 8
    return struct.pack('>%dH' % HDR, *w) + geo


def icon_group(points):
    body = b''.join(struct.pack('>HHHH', typ, 0xffff, x, y) for typ, x, y in points)
    g = struct.pack('>HHH', 1, 3, len(points)) + body
    return g + b'\0' * (-len(g) % 8)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--icon', required=True, help='point type code, e.g. 0x806c')
    ap.add_argument('--check', action='store_true', help='rewrite only, must equal IN')
    a = ap.parse_args(argv)
    icon = int(a.icon, 0)

    d = open(a.src, 'rb').read()
    doff, rows = read_rows(d)
    if a.check:
        out = write_rows(d, doff, rows)
        print('rewrite equals input:', out == d, len(d), len(out))
        return
    cell = h.cell_of(a.lon, a.lat)
    assert h.file_name(*cell) == os.path.basename(a.src).split('.')[0] + '.DAT', 'wrong HOUSE001 file for the point'
    lon0, lat0, cw, ch = h.cell_geo(*cell)
    pw, ph = cw / 16, ch / 16
    k, s = int((a.lon - lon0) / pw), int((a.lat - lat0) / ph)
    x = round((a.lon - lon0 - k * pw) / pw * 2048)
    y = round((a.lat - lat0 - s * ph) / ph * 2048)
    plain = rows[s][2 * k]
    w, layers = layers_of(plain)
    assert 10 not in layers, 'parcel already has layer 10'
    layers[10] = icon_group([(icon, x, y)])
    rows[s][2 * k] = record_of(w, layers)
    out = write_rows(d, doff, rows)
    open(a.dst, 'wb').write(out)
    print('parcel s=%d k=%d: layer 10 = 0x%04x at (%d,%d); record %d -> %d bytes, file %d -> %d'
          % (s, k, icon, x, y, len(plain), len(rows[s][2 * k]), len(d), len(out)))


if __name__ == '__main__':
    main(sys.argv[1:])
