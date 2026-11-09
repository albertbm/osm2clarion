#!/usr/bin/env python3
"""Survey section 4 of a tile: which parcels carry a record, and what country.

Section 4 is the per-parcel administrative index. Records are sparse, so the first
question about any tile is which parcels have one. Compares the section-4 parcel set
against section 1 and section 2 at the same level so the rule is visible.

    python3 tools/sec4_scan.py B68R058R.DAT [--list] [--cmp]
"""
import struct, sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan


def dir_head(t, off):
    kind, a, b, y, x, typ = struct.unpack_from('>6H', t.m, off)
    return kind, a, b, y, x, typ


def parcel_of(p):
    """(row, col) from a record's own parcel code words. The column is only the low
    14 bits: the code has 8 bits for col>>6, so col 30169 reads back as 13785."""
    w0, w1 = struct.unpack_from('>HH', p, 0)
    row = ((w0 >> 8) & 0x3f) << 6 | ((w0 >> 4) & 0xf) << 2 | (w0 & 3)
    col = ((w1 >> 8) & 0xff) << 6 | ((w1 >> 4) & 0xf) << 2 | (w1 & 3)
    return row, col


def parcels(t, section, level):
    """{(row, col)} for every parcel with a record in that section and level."""
    yx = {}
    out = set()
    for sid, lev, doff, _ in t._dirs(section):
        if lev == level:
            h = dir_head(t, doff)
            yx[doff] = (h[3], h[4])
    for r in t.records(section=section, level=level):
        y, x = yx[r.dir_off]
        col = r.k // 2 if section == 1 else r.k   # section 1 indexes 2*col+1
        out.add((y * 32 + r.slot, x * 32 + col))
    return out


def main(path, do_list, do_cmp):
    with TileScan(path) as t:
        print(os.path.basename(path), dict(t.sections()))
        for sid, lev, doff, _ in t._dirs(4):
            print('  section 4 level %d dir at %#x head %s' % (lev, doff, dir_head(t, doff)))
        recs, bad = {}, collections.Counter()
        for r in t.records(section=4, level=1):
            try:
                p = r.plain()
            except Exception:
                bad['inflate failed'] += 1
                continue
            if len(p) < 18:
                bad['short'] += 1
                continue
            kind = struct.unpack_from('>H', p, 4)[0]
            if kind != 0x0009:
                bad['kind %#06x' % kind] += 1
                continue
            _, _, _, y, x, _ = dir_head(t, r.dir_off)
            abs_rc = (y * 32 + r.slot, x * 32 + r.k)
            own = parcel_of(p)
            cc = struct.unpack_from('>H', p, 16)[0]
            recs[abs_rc] = (own, cc, len(p),
                            own == (abs_rc[0], abs_rc[1] & 0x3fff))
        print('  kind 0x0009 records: %d   skipped: %s' % (len(recs), dict(bad)))
        print('  parcel code agrees with directory position: %d of %d'
              % (sum(1 for v in recs.values() if v[3]), len(recs)))
        print('  countries: %s' % dict(collections.Counter(v[1] for v in recs.values())))
        print('  lengths: %s' % dict(collections.Counter(v[2] for v in recs.values())))
        if do_list:
            for rc in sorted(recs):
                own, cc, n, ok = recs[rc]
                print('    row %5d col %5d  cc %4d  plain %5d  %s' % (rc[0], rc[1], cc, n, 'ok' if ok else own))
        if do_cmp:
            s4 = set(recs)
            for sid in (1, 2):
                so = parcels(t, sid, 1)
                print('  vs section %d level 1: s%d parcels=%d  s4=%d  s4 also in s%d=%d  '
                      's%d not in s4=%d'
                      % (sid, sid, len(so), len(s4), sid, len(s4 & so), sid, len(so - s4)))


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    for p in args:
        main(p, '--list' in sys.argv, '--cmp' in sys.argv)
