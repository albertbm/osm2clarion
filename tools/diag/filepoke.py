#!/usr/bin/env python3
"""Patch bytes at file offsets inside a card image without rewriting the file.

Walks the FAT to the file, maps each file offset to an image offset through the cluster
chain, and writes there. For a 322 MB uncompressed file a handful of name bytes would
otherwise mean a full extract and rewrite.

    filepoke.py IMAGE PATH --dry-run|--write OFFSET=HEX [OFFSET=HEX ...]
    filepoke.py IMAGE PATH --show OFFSET:LEN [...]
"""
import sys, os, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fatread import Fat


def locate(img, path):
    f = Fat(img)
    f.open_part(f.parts[0][2])
    clus = size = None
    for comp in path.split('/'):
        m = [e for e in f.readdir(clus) if e[0].upper() == comp.upper()]
        if not m:
            sys.exit('not found: %s' % comp)
        clus, size = m[0][2], m[0][3]
    return f, clus, size


def mapper(f, clus, size):
    """file offset -> absolute image offset, via a cached cluster list."""
    csz = f.spc * f.bps
    chain, c = [], clus
    while c and c < 0x0ffffff8 and len(chain) * csz < size:
        chain.append(c)
        c = f.fat_entry(c)
    def at(off):
        if off >= size:
            sys.exit('offset %d past end of file (%d)' % (off, size))
        return f.csec(chain[off // csz]) * f.bps + off % csz
    return at, csz


def main():
    img, path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    f, clus, size = locate(img, path)
    at, csz = mapper(f, clus, size)
    print('%s  %d bytes, first cluster %d, cluster size %d' % (path, size, clus, csz))
    if mode == '--show':
        for a in sys.argv[4:]:
            off, n = (int(x, 0) for x in a.split(':'))
            if off // csz != (off + n - 1) // csz:
                sys.exit('range %s crosses a cluster boundary' % a)
            f.f.seek(at(off))
            print('  %10d -> image %12d  %s' % (off, at(off), f.f.read(n).hex()))
        return
    pokes = []
    for a in sys.argv[4:]:
        off, hx = a.split('=')
        off, data = int(off, 0), bytes.fromhex(hx)
        if off // csz != (off + len(data) - 1) // csz:
            sys.exit('write at %d crosses a cluster boundary' % off)
        pokes.append((off, at(off), data))
    for off, iof, data in pokes:
        f.f.seek(iof)
        print('  %10d -> image %12d  %d B  was %s  -> %s'
              % (off, iof, len(data), f.f.read(len(data)).hex(), data.hex()))
    if mode != '--write':
        print('dry run, nothing written')
        return
    g = open(img, 'r+b')
    for off, iof, data in pokes:
        g.seek(iof)
        g.write(data)
    g.flush()
    os.fsync(g.fileno())
    g.close()
    print('wrote %d ranges, %d bytes, fsynced' % (len(pokes), sum(len(d) for _, _, d in pokes)))


main()
