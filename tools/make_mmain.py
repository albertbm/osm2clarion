#!/usr/bin/env python3
"""MMAIN001.DAT for a rebuilt database.

MMAIN001.DAT is the 4 KiB master file: the tile header block with the database
bounds at 0x184 and 0x5d0, and a level table at 0x1ac whose counts are the sums
of every tile's level table (plus LEV3456R.DAT's). Everything else is copied
from the card's own MMAIN001.DAT.

    ./make_mmain.py --profile iceland CARD_MMAIN001.DAT OUT.DAT TILE.DAT... [--lev LEV3456R.DAT]
    --bounds latmax,latmin,lonmin,lonmax   overrides the profile's
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_profile
from clarion_tile import walk


def main():
    argv = sys.argv[1:]
    BOUNDS = clarion_profile.take_arg(argv).bounds
    lev = None
    if '--lev' in argv:
        i = argv.index('--lev'); lev = argv[i + 1]; del argv[i:i + 2]
    if '--bounds' in argv:
        i = argv.index('--bounds'); BOUNDS = tuple(int(v) for v in argv[i + 1].split(',')); del argv[i:i + 2]
    args = argv
    src, dest, tiles = args[0], args[1], args[2:]
    d = bytearray(open(src, 'rb').read())

    totals = {}   # (section, level) -> (type, count)
    for fn in tiles + ([lev] if lev else []):
        h = open(fn, 'rb').read(0x400)
        for sid, ents in walk(h, 0x1ac, 4)[0]:
            for e in ents:
                level, typ, cnt = e[0], e[1], struct.unpack('>H', e[2:])[0]
                t, c = totals.get((sid, level), (typ, 0))
                totals[(sid, level)] = (typ, c + cnt)

    card, end = walk(d, 0x1ac, 4)
    out = struct.pack('>H', len(card))
    for sid, ents in card:
        out += bytes([sid, len(ents)])
        for e in ents:
            level, typ = e[0], e[1]
            if not any(k[0] == sid for k in totals):   # section absent from our files: leave the card's value
                cnt = struct.unpack('>H', e[2:])[0]
            else:
                _, cnt = totals.get((sid, level), (typ, 0))
            out += struct.pack('>BBH', level, typ, cnt)
            print(f'section {sid} level {level} type {typ:#x}: {struct.unpack(">H", e[2:])[0]} -> {cnt}')
    d[0x1ac:0x1ac + len(out)] = out
    d[0x1ac + len(out):end] = b'\0' * (end - 0x1ac - len(out)) if end > 0x1ac + len(out) else b''
    struct.pack_into('>hhhh', d, 0x184, *BOUNDS)
    struct.pack_into('>hh', d, 0x5d0, BOUNDS[1], BOUNDS[2])
    open(dest, 'wb').write(d)
    print('wrote', dest, len(d))


if __name__ == '__main__':
    main()
