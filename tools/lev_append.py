#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Append level-3 directories for new cells to LEV3456R.DAT.

LEV3456R.DAT holds levels 3-6 for the whole database. Level 3 is one directory
per tile cell (16 rows of 20' x 16 columns of 30'). Levels 4-6 are single
whole-database directories whose slot layout is not decoded, so this tool
leaves every existing byte in place and only:
  - appends the new directories and their record blocks at the end of the file,
  - appends their entries to the level-3 group of the section table at 0x1000
    (the group is the last thing in that table and there is room before 0x1300),
  - bumps the level-3 count in the level table at 0x1ac,
  - sets the database bounds at 0x184 and 0x5d0, but only when asked: with a shift it is
    lev_coarse that moves them, and it needs to see the old values to shift the per-level
    boxes by the same amount.

    ./lev_append.py --profile iceland IN.DAT OUT.DAT [CELL...]
    --coastline PATH     overrides the profile's
    --bounds latmax,latmin,lonmin,lonmax
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_tile
from clarion_tile import walk, enc_addr, pad256, dir_bytes, block_bytes

def append_dirs(d, dirs, bounds=None):
    d = bytearray(d)
    lv, lv_end = walk(d, 0x1ac, 4)
    # level table: section 1, level 3 count
    p = 0x1ac + 2
    for sid, ents in lv:
        p += 2
        for e in ents:
            if sid == 1 and e[0] == 3:
                struct.pack_into('>H', d, p + 2, struct.unpack('>H', e[2:])[0] + len(dirs))
            p += 4
    # section table
    taddr = clarion_tile.addr_of(struct.unpack_from('>I', d, 0x800)[0])
    nw = struct.unpack_from('>H', d, taddr)[0]
    w = list(struct.unpack_from('>%dH' % nw, d, taddr))
    nlev = len(lv[0][1])
    l3 = [i for i, e in enumerate(lv[0][1]) if e[0] == 3][0]
    o = w[1 + l3]
    glen, gcnt = w[o], w[o + 1]
    assert o + glen == nw, 'level-3 group is not the last group in the table'
    # place new objects at the end of the file
    pos = pad256(len(d))
    out = bytearray()
    entries = []
    for D in dirs:
        doff = pos + len(out)
        dsize = 12 + D['nslots'] * (4 + D['body'])
        boff = pad256(doff + dsize)
        blocks = bytearray(); slot_addrs = {}
        for i in sorted(D['slots']):
            slot_addrs[i] = boff + len(blocks)
            b = block_bytes(D['slots'][i]); blocks += b; blocks += b'\0' * (pad256(len(b)) - len(b))
        db = dir_bytes(D, slot_addrs)
        out += db; out += b'\0' * (boff - doff - len(db)); out += blocks
        a = enc_addr(doff)
        entries += [D['box'][0], D['box'][1], D['box'][2], D['box'][3], a >> 16, a & 0xffff]
    # merge the new entries into the group keeping it sorted by (ymin, xmin), as shipped
    old = [w[o + 2 + 6*k:o + 8 + 6*k] for k in range(gcnt)]
    new = [entries[6*k:6*k + 6] for k in range(len(dirs))]
    merged = sorted(old + new, key=lambda e: (e[1], e[2]))
    w = w[:o] + [glen + 6 * len(dirs), gcnt + len(dirs)] + [v for e in merged for v in e]
    w[0] = nw + 6 * len(dirs)
    new_table = struct.pack('>%dH' % len(w), *w)
    next_obj = min(clarion_tile.addr_of((w[o + 6 + 6*k] << 16) | w[o + 7 + 6*k]) for k in range(gcnt)) if gcnt else len(d)
    first_dir = 0x1300
    assert taddr + len(new_table) <= first_dir, 'section table would overrun the first directory'
    d[taddr:taddr + len(new_table)] = new_table
    if bounds:
        struct.pack_into('>hhhh', d, 0x184, *bounds)
        struct.pack_into('>hh', d, 0x5d0, bounds[1], bounds[2])
    d += b'\0' * (pos - len(d)); d += out
    return bytes(d)

def main():
    from shapely.geometry import box
    import bg_encode, bg_tiles, clarion_profile
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    bg_tiles.use_profile(P)
    # Not P.bounds: the bounds are written by whichever step moves the grid, which is
    # lev_coarse. Writing them here too leaves lev_coarse with a zero shift to apply to
    # the per-level box table at 0x2ac.
    bounds = None
    coast_path = P.coastline
    if '--bounds' in argv:
        i = argv.index('--bounds'); bounds = tuple(int(v) for v in argv[i + 1].split(',')); del argv[i:i + 2]
    if '--coastline' in argv:
        i = argv.index('--coastline'); coast_path = argv[i + 1]; del argv[i:i + 2]
    src, dest = argv[0], argv[1]
    cells = argv[2:] or P.cells
    land, coast, tree = bg_tiles.load_land(coast_path)
    land, coast, tree = bg_tiles.clean_land(land, 3)
    dirs = []
    for name in cells:
        yb, xb = bg_encode.tile_cell(name)
        cell = box(bg_encode.mesh_to_lon(xb), bg_encode.mesh_to_lat(yb),
                   bg_encode.mesh_to_lon(xb + 8), bg_encode.mesh_to_lat(yb + 8))
        land_local = land.intersection(cell) if land.intersects(cell) else box(0, 0, 0, 0).buffer(0)
        recs = bg_tiles.encode_level_fast(3, yb, xb, land_local, coast, tree, cell)
        D = bg_encode.make_dir(3, yb, xb, recs)
        print(name, 'level 3 parcels', sum(len(c) for c in recs.values()))
        dirs.append(D)
    d = open(src, 'rb').read()
    open(dest, 'wb').write(append_dirs(d, dirs, bounds))
    print('wrote', dest)


if __name__ == '__main__':
    main()
