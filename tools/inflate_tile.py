# SPDX-License-Identifier: GPL-3.0-or-later
"""Inflate every zlib record of a tile into files, one per record.

    ./inflate_tile.py TILE.DAT OUTDIR

File names carry the directory offset, level type, mesh, slot (parcel row) and pair index,
with _e1 to _e4 on the extra records of an extended slot.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_format
from clarion_tile import read_tile

def main():
    fn, outdir = sys.argv[1], sys.argv[2]
    d = open(fn, 'rb').read()
    os.makedirs(outdir, exist_ok=True)
    n = 0
    fmt = clarion_format.of_header(d[:0x1000])
    for sec in read_tile(d)['sections']:
        for L in sec['levels']:
            for D in L['dirs']:
                for i, s in D['slots'].items():
                    p = s['block_off']
                    for r in s['recs']:
                        name = f"{outdir}/d{D['off']:06x}_t{D['typ']:02x}_y{D['y']}_x{D['x']}_s{i:02d}_k{r['k']:02d}{'_e%d' % r['sub'] if r.get('sub') else ''}_@{p:06x}"
                        if fmt.is_zlib(r['raw']):
                            out = fmt.inflate(r['raw'])
                            assert len(out) == 2 * r['words']
                            open(name + '.bin', 'wb').write(out)
                        else:
                            open(name + '.raw', 'wb').write(r['raw'])
                        p += len(r['raw']); n += 1
    print(fn, 'wrote', n, 'records')

if __name__ == '__main__':
    import cli_help; cli_help.help_exit(__doc__)
    main()
