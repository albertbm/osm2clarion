#!/usr/bin/env python3
"""Move Iceland's level-4 road shapes from layer 5 to layer 6 in a built LEV3456R.DAT.

`lev_roads` used to write the layer pair at unit[10]/unit[11], which is layer 5. No shipped
level-4 record puts road shapes there: the 2014 card's 359 road-block records keep classes 3-6
in layer 6 (236,517 shapes) and classes 1-2 in layer 7 (31,864), and layer 5 is empty.
`lev_roads.py` writes layer 6 now, so a fresh build does not need this. It is for a coarse
file that was already built, where a full rerun would also pull in the bounds and
geometry-slot fixes made since. Moving the pair touches nothing else: the record's plain
length does not change, only the four words of the layer table.

    ./lev4_layer.py IN.DAT OUT.DAT
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lev_coarse


def patch_record(plain):
    """Swap the layer-5 pair for the layer-6 pair in a level-4 road unit. None if nothing to do."""
    w = list(struct.unpack_from('>22H', plain, 0))
    if w[7] == 0xffff or not w[8]:
        return None
    base = 4 * w[7]
    tab = list(struct.unpack_from('>22H', plain, base))
    if tab[10] == 0xffff or not tab[11]:
        return None
    assert tab[12] == 0xffff and not tab[13], 'layer 6 is already in use: %r' % tab[12:14]
    tab[12], tab[13] = tab[10], tab[11]
    tab[10], tab[11] = 0xffff, 0x0000
    out = bytearray(plain)
    struct.pack_into('>22H', out, base, *tab)
    return bytes(out)


def main():
    src, dest = sys.argv[1], sys.argv[2]
    d = bytearray(open(src, 'rb').read())
    D = lev_coarse.read_dir(d, 4)
    moved = 0
    for row, s in enumerate(D['slots']):
        for k, r in enumerate(s['recs']):
            if r is None or r[:2] != b'\x58\x85':
                continue
            plain = zlib.decompress(bytes(r))
            new = patch_record(plain)
            if new is None:
                continue
            assert len(new) == len(plain)
            c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
            comp = c.compress(new) + c.flush()
            if len(comp) % 2:
                comp += b'\0'
            s['recs'][k] = comp
            s['pairs'][2 * k], s['pairs'][2 * k + 1] = len(comp) // 2, len(new) // 2
            s['dirty'] = True
            moved += 1
    lev_coarse.write_dir(d, D)
    open(dest, 'wb').write(d)
    print(f'{moved} level-4 records moved from layer 5 to layer 6; wrote {dest} {len(d)} bytes')


if __name__ == '__main__':
    main()
