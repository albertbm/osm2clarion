#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the grid formula against a directory of tiles.

A tile's name encodes its south-west corner in hexadecimal: lat_min = B * 2/3 degrees
and lon_max = 108 - R. Established across all 52 tiles of a European card. This reads
each tile's own bounding box out of its header and reports any that disagree, then lists
the outliers by size.

    ./tile_grid.py [DIR]
"""
import os, struct, sys


def sgn(v):
    return v - 65536 if v > 32767 else v


def boxes(dirname):
    """(name, B, R, lat_min, lat_max, lon_min, lon_max, bytes) per tile."""
    rows = []
    for f in sorted(os.listdir(dirname)):
        if not f.startswith('B'):
            continue
        path = os.path.join(dirname, f)
        d = open(path, 'rb').read(0x600)
        i = 0x1e0
        while i < 0x600 and d[i] == 0xff:
            i += 1
        a, b, c, dd = struct.unpack('>HHHH', d[i+6:i+14])
        rows.append((f, int(f[1:3], 16), int(f[4:7], 16),
                     sgn(b) / 60, sgn(a) / 60, sgn(c) / 60, sgn(dd) / 60,
                     os.path.getsize(path)))
    return rows


def main():
    rows = boxes(sys.argv[1] if len(sys.argv) > 1 else '.')
    print('checking  lat_min = B*2/3   and   lon_max = 108 - R')
    bad = 0
    for f, B, R, latlo, lathi, lonlo, lonhi, sz in rows:
        plat, plon = B * 2 / 3, 108 - R
        if not (abs(latlo - plat) < 5.4 and abs(lonhi - plon) < 8.1):
            bad += 1
            print('  MISS', f, latlo, lathi, lonlo, lonhi, 'predicted', plat, plon)
    print('tiles:', len(rows), ' outside predicted cell:', bad)
    print()
    print('%-14s %-18s %-18s %7s' % ('tile', 'lat', 'lon', 'MB'))
    for f, B, R, latlo, lathi, lonlo, lonhi, sz in rows:
        if sz > 2e8 or sz < 1e6:          # only the extremes are interesting
            print('%-14s %6.2f..%-8.2f %7.2f..%-8.2f %7.1f'
                  % (f, latlo, lathi, lonlo, lonhi, sz / 1e6))


if __name__ == '__main__':
    main()
