"""Inflate every zlib record of a tile into files, one per record.

    ./inflate_tile.py TILE.DAT OUTDIR

File names carry the directory offset, level type, mesh, slot (parcel row) and pair index.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clarion_tile import read_tile

def main():
    fn, outdir = sys.argv[1], sys.argv[2]
    d = open(fn, 'rb').read()
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for sec in read_tile(d)['sections']:
        for L in sec['levels']:
            for D in L['dirs']:
                for i, s in D['slots'].items():
                    p = s['block_off']
                    for r in s['recs']:
                        name = f"{outdir}/d{D['off']:06x}_t{D['typ']:02x}_y{D['y']}_x{D['x']}_s{i:02d}_k{r['k']:02d}_@{p:06x}"
                        if r['raw'][:2] == b'\x58\x85':
                            out = zlib.decompress(r['raw'])
                            assert len(out) == 2 * r['words']
                            open(name + '.bin', 'wb').write(out)
                        else:
                            open(name + '.raw', 'wb').write(r['raw'])
                        p += len(r['raw']); n += 1
    print(fn, 'wrote', n, 'records')

if __name__ == '__main__':
    main()
