#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild a tile from its decoded parts and say whether it comes out the same.

The header is written again from its fields, and every compressed record is inflated and
deflated again in the format the tile names (clarion_format). Directories, section tables
and uncompressed records are copied. This is the codec's round trip on a factory tile.

    tile_recode.py SRC OUT

It reads through tile_scan, so a factory tile costs its directories, not seven times its
size. A record that deflates to another length would move everything after it, and that
needs clarion_tile.write_tile, so the tool stops there instead.
"""
import hashlib
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tile_header
from tile_scan import TileScan


def recode(src, out):
    """Write the rebuilt tile to out. Returns (format, records, recompressed, [differing records])."""
    shutil.copyfile(src, out)
    differ, nrec, nz = [], 0, 0
    with TileScan(src) as t, open(out, 'r+b') as o:
        fmt = t.fmt
        h = bytes(t.m[:0x1000])
        f, sect = tile_header.fields(h)
        b = tile_header.author(**f, fmt=fmt)
        for i, (a, c) in enumerate(sect):
            struct.pack_into('>IH', b, tile_header.SECT_OFF + 6 * i, a, c)
        o.write(b)
        for r in t.records():
            nrec += 1
            if not r.is_zlib:
                continue
            nz += 1
            raw = r.raw()
            z = fmt.deflate(fmt.inflate(raw))
            if len(z) != len(raw):
                raise SystemExit('%s: %r deflates to %d bytes, not %d; a relayout needs write_tile'
                                 % (src, r, len(z), len(raw)))
            if z != raw:
                differ.append(r)
            o.seek(r.off)
            o.write(z)
    return fmt, nrec, nz, differ


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, out = sys.argv[1:]
    fmt, nrec, nz, differ = recode(src, out)
    same = sha(src) == sha(out)
    print('%s: %s, %d records, %d recompressed, %d differ; rebuilt tile %s'
          % (src, fmt, nrec, nz, len(differ), 'byte-identical' if same else 'DIFFERS'))
    for r in differ[:20]:
        print('   ', r)
    sys.exit(0 if same else 1)
