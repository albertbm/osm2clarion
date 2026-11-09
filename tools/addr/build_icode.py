"""Put the 21 Icelandic towns into REFER001/ICODE001.DAT under area 12.

Iceland-specific, so it stays in leaf-is-map; the record layout it writes lives in
osm2clarion's icode_io.

Area 12 is Italy's ordinal, the row the card renames to Iceland. An Icelandic address
is street and house number, postcode, town, country, so area 12's ISO entry is cut to
three tier labels and the tree under it is the 21 towns with the same name repeated
beneath each, which is the shape the Vatican, Monaco, San Marino and Andorra already
have. `(12, town, 0)` then resolves to ISLAND / KOPAVOGUR / KOPAVOGUR.

Italy's own twenty region rows and everything under them stay byte-identical. The new
subtree goes at the end of the space Italy's subtree occupies, which nothing reaches
once area 12's country row points at the town chain instead, and the country row's
`roff` and `cnt` are the only bytes of the chain it sits in that change.

    python3 tools/addr/build_icode.py <ICODE001.DAT on the card> <pristine ICODE001.DAT>
                                      <out poke file> [--patched FILE]
"""
import argparse
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import icode_io  # noqa: E402
from addr_csv import check_ladder  # noqa: E402

AREA = 12          # Italy's ordinal; the row the card renames to Iceland
LABELS = ('COUNTRY', 'SVEITARFÉLAG(MUNICIPALITY)', 'SETTLEMENT')
ROW_ROFF = 4       # a row's child-chain offset, bytes into the row
ROW_CNT = 8        # that chain's size in halfwords


def language_tables(d):
    """Every country chain in the file: a 42-row chain whose rows index 0..41.

    Found by offset rather than by name, because the name differs per language and the
    tree is recursive, so chain shape alone does not identify a country chain. The 19
    bases are the ones `REPORT-italy-consolidation.md` section 2a measured.
    """
    TOP = struct.pack('>HHII', icode_io.TERM, 0, 0xffffffff, 0xffffffff)
    bases = []
    end = d.find(TOP)
    while end >= 0:
        for p in range(end - 12, max(0, end - 0x4000), -2):
            rlen, nlen, _roff, _cnt, idx = struct.unpack_from('>HHIHH', d, p)
            if idx or nlen == 0 or (rlen & 0x7fff) != nlen + 6:
                continue
            rows = icode_io.read_list(d, p)
            if len(rows) == 42 and [r.idx for r in rows] == list(range(42)) \
                    and rows[-1].off + rows[-1].rlen * 2 == end:
                bases.append(p)
                break
        end = d.find(TOP, end + 1)
    return bases


def chain_end(d, off):
    """The byte just past a chain's terminator or terminator pair."""
    rows = icode_io.read_list(d, off)
    o = rows[-1].off + rows[-1].rlen * 2 if rows else off
    for _ in range(2):
        _rlen, nlen, _roff, _cnt, _idx = struct.unpack_from('>HHIHH', d, o)
        if nlen:
            break
        o += 12
    return o


def subtree_extent(d, base, row, depth):
    """(first byte, last byte) of everything under one row, to `depth` levels."""
    if row.nosub or not row.cnt:
        return None
    off = base + row.roff * 2
    lo, hi = off, chain_end(d, off)
    if depth > 1:
        for kid in icode_io.read_list(d, off):
            sub = subtree_extent(d, base, kid, depth - 1)
            if sub:
                lo, hi = min(lo, sub[0]), max(hi, sub[1])
    return lo, hi


def restore_span(cur, pristine, start, settle=0x1000, cap=0x40000):
    """How far the card's copy differs from the pristine one, from `start`.

    The previous build rewrote area 12's region chain and wrote four town chains after
    it, in one run. Everything in that run goes back to what the factory wrote. The
    run's diffs are sparse, because its first twenty rows are Italy's own unchanged
    ones, so the end is the last differing byte before a long identical stretch rather
    than the first identical byte. `settle` is that stretch; the caller checks nothing
    else differs for a long way past it, which is how a second run - the Pantelleria
    rename, about a megabyte further on - is kept out of the restore.
    """
    last = None
    o = start
    while o < start + cap:
        if cur[o] != pristine[o]:
            last = o
        elif last is not None and o - last >= settle:
            break
        o += 1
    return 0 if last is None else last - start + 1


def next_diff(cur, pristine, start, cap=0x100000):
    """The first byte at or after `start` where the two copies differ, or None."""
    for o in range(start, min(len(cur), start + cap)):
        if cur[o] != pristine[o]:
            return o
    return None


def town_chains(towns, base, start, country_roff):
    """(level-1 chain, [(byte offset, chain)], bytes used) for one language table."""
    names = [t.upper() for t in towns]
    kids = [icode_io.pack_list([icode_io.pack_row(n, 0, 0, 0, leaf=True)],
                               0, deepest=True) for n in names]
    # the level-1 chain's own size does not depend on where its children land, so one
    # pass sizes it and the second fills the offsets in
    probe = icode_io.pack_list([icode_io.pack_row(n, 0, 0, i) for i, n in enumerate(names)], 0)
    pos = start + len(probe)
    pos += -pos % 4
    placed = []
    for c in kids:
        placed.append((pos, c))
        pos += len(c)
        pos += -pos % 4
    rows = [icode_io.pack_row(n, (placed[i][0] - base) // 2, len(placed[i][1]) // 2, i)
            for i, n in enumerate(names)]
    level1 = icode_io.pack_list(rows, country_roff)
    assert len(level1) == len(probe), (len(level1), len(probe))
    return level1, placed, pos - start


def plan_table(cur, pristine, base, towns):
    """Pokes for one language table. Returns (list of (offset, bytes), notes)."""
    rows = icode_io.read_list(cur, base)
    assert len(rows) == 42, (base, len(rows))
    country = rows[AREA]
    rl_off = base + country.roff * 2

    span = restore_span(cur, pristine, rl_off)
    pokes = [(rl_off, bytes(pristine[rl_off:rl_off + span]))] if span else []
    # the next thing that differs must be far away, so the restore is this one run and
    # not the leading edge of the Pantelleria rename
    after = next_diff(cur, pristine, rl_off + span)
    assert after is None or after - (rl_off + span) > 0x40000, (rl_off, span, after)

    # Italy's subtree, measured on the pristine copy because that is what the restore
    # puts back. Five labels, so four levels of chain hang under the country row.
    italy = subtree_extent(pristine, base, icode_io.read_list(pristine, base)[AREA], 4)
    assert italy, base
    it_lo, it_hi = italy
    need = town_chains(towns, base, it_hi, 0)[2]
    start = (it_hi - need) & ~3
    assert start > rl_off + span, (start, rl_off, span)

    level1, placed, used = town_chains(towns, base, start, 0)
    # nothing already patched on the card may sit where the chains land, or the restore
    # and the write would fight over the same bytes
    collide = next_diff(cur, pristine, start, used)
    assert collide is None, (start, used, collide)
    pokes.append((start, level1))
    pokes += placed
    pokes.append((country.off + ROW_ROFF, struct.pack('>I', (start - base) // 2)))
    pokes.append((country.off + ROW_CNT, struct.pack('>H', len(level1) // 2)))
    notes = dict(base=base, country=country.name.decode('utf-8', 'replace'),
                 restored=span, start=start, used=used, it_lo=it_lo, it_hi=it_hi,
                 old_roff=country.roff, new_roff=(start - base) // 2,
                 old_cnt=country.cnt, new_cnt=len(level1) // 2)
    return pokes, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('icode', help='the copy on the card, which the pokes apply to')
    ap.add_argument('pristine', help='the same file before any Iceland patch')
    ap.add_argument('pokefile')
    ap.add_argument('--patched', help='also write a patched copy of the whole file')
    a = ap.parse_args()
    cur = open(a.icode, 'rb').read()
    pristine = open(a.pristine, 'rb').read()
    assert len(cur) == len(pristine), (len(cur), len(pristine))
    towns, town_of = check_ladder()
    out = [(icode_io.iso_slots_off(AREA), icode_io.iso_slots(list(LABELS)))]
    print('ISO entry %d: %s' % (AREA, ' | '.join(LABELS)))
    for b in language_tables(cur):
        pokes, n = plan_table(cur, pristine, b, towns)
        out += pokes
        print('  base %9d %-12s restored %4d B  subtree %d..%d  chains at %d (%d B)'
              '  roff %d->%d cnt %d->%d' % (
                  n['base'], n['country'], n['restored'], n['it_lo'], n['it_hi'],
                  n['start'], n['used'], n['old_roff'], n['new_roff'],
                  n['old_cnt'], n['new_cnt']))
    with open(a.pokefile, 'w') as f:
        for off, b in out:
            f.write('%d=%s\n' % (off, b.hex()))
    print('%d pokes, %d bytes -> %s' % (len(out), sum(len(b) for _, b in out), a.pokefile))
    print('town rows 0..%d: %s' % (len(towns) - 1, ', '.join(towns)))
    if a.patched:
        buf = bytearray(cur)
        for off, b in out:
            buf[off:off + len(b)] = b
        assert len(buf) == len(cur)
        Path(a.patched).write_bytes(bytes(buf))
        print('patched copy ->', a.patched)


if __name__ == '__main__':
    main()
