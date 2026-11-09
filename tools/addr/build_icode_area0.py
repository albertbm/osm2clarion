"""Cut every language's country chain to one record, Iceland, at ordinal 0.

Iceland-specific, so it stays in leaf-is-map; the record layout it writes lives in
osm2clarion's icode_io.

`build_icode.py` is the area-12 version of this: it keeps all 42 country rows and
repoints Italy's. This one drops the other 41 rows. The country list is the first-level
chain of each language's area tree, walked by `rlen` to a terminator, so a one-record
chain plus a terminator is the whole list, and the volume record's first-level category
size at `0x5bfc + 26k + 18` has to agree or the boot reads past the end.

The surviving row takes `idx` 0, because the unit only opens the list when some row's
`idx` matches the ordinal it has stored, and only draws a name when `idx` is below the
record count. Ordinal 0 is the one value that satisfies both with one record. See
`REPORT-country-list.md`.

The row keeps the name the base card already carries at ordinal 12, so each language
reads what it read before. Its subtree is written over the head of the old ordinal-0
subtree, which nothing reaches once row 0 points at it instead: one region row and one
settlement row under it, three levels counting the country, matching the three tier
labels the ISO entry gets.

    python3 tools/addr/build_icode_area0.py <ICODE001.DAT> <out poke file> [--patched FILE]
"""
import argparse
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import icode_io  # noqa: E402
from build_icode import language_tables  # noqa: E402

AREA = 0           # Albania's ordinal, the slot the one-record list takes over
KEEP = 12          # the row whose name the surviving record keeps
ISO = 352
LABELS = ('COUNTRY', 'SVEITARFÉLAG(MUNICIPALITY)', 'SETTLEMENT')
REGION = 'REYKJAVÍK'
VOLUME = 0x5bfc    # the volume table
VREC = 26          # one volume record
VFLC = 18          # first-level category size, halfwords, inside a volume record


def volume_records(d):
    """{data-frame byte address: record offset} for the area frames of the volume table."""
    out = {}
    k = 0
    while True:
        p = VOLUME + VREC * k
        disp, sa = struct.unpack_from('>II', d, p + 2)
        kind = d[p + 23]
        if kind == 1:
            out[(sa >> 8) * 2048 + (sa & 0x3f) * 256] = p
        if disp == 0xffffffff:
            return out
        k += 1


def plan_table(d, base):
    """Pokes for one language table, plus a note dict."""
    rows = icode_io.read_list(d, base)
    assert len(rows) == 42, (base, len(rows))
    label = rows[KEEP].name                    # the name this language already shows
    sub = rows[AREA].roff * 2                  # head of the old ordinal-0 subtree

    leaf = icode_io.pack_list([icode_io.pack_row(REGION, 0, 0, 0, leaf=True)],
                              sub // 2, deepest=True)
    lvl1_at = sub
    lvl2_at = (sub + 34 + 3) & ~3              # the level-1 chain is always 34 bytes
    lvl1 = icode_io.pack_list(
        [icode_io.pack_row(REGION, lvl2_at // 2, len(leaf) // 2, 0)], 0)
    assert len(lvl1) == 34, len(lvl1)

    top = icode_io.pack_list([icode_io.pack_row(label, lvl1_at // 2, len(lvl1) // 2, 0)], 0xffffffff)
    pokes = [(base, top), (base + lvl1_at, lvl1), (base + lvl2_at, leaf)]
    return pokes, dict(base=base, name=label.decode('utf-8', 'replace'), top=len(top),
                       flc=len(top) // 2, lvl1=lvl1_at, lvl2=lvl2_at)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('icode')
    ap.add_argument('pokefile')
    ap.add_argument('--patched')
    a = ap.parse_args()
    d = open(a.icode, 'rb').read()
    vol = volume_records(d)

    out = [(icode_io.ISO_TABLE + 4 + AREA * icode_io.ENTRY + 2, struct.pack('>H', ISO)),
           (icode_io.iso_slots_off(AREA), icode_io.iso_slots(list(LABELS)))]
    print('ISO entry %d: %d  %s' % (AREA, ISO, ' | '.join(LABELS)))
    for b in language_tables(d):
        pokes, n = plan_table(d, b)
        out += pokes
        vrec = vol[b]
        out.append((vrec + VFLC, struct.pack('>H', n['flc'])))
        old = struct.unpack_from('>H', d, vrec + VFLC)[0]
        print('  base %9d  %-10s chain %2d B (flc %d->%d at %#x)  level1 +%d  level2 +%d'
              % (n['base'], n['name'], n['top'], old, n['flc'], vrec + VFLC,
                 n['lvl1'], n['lvl2']))
    with open(a.pokefile, 'w') as f:
        for off, b in out:
            f.write('%d=%s\n' % (off, b.hex()))
    print('%d pokes, %d bytes -> %s' % (len(out), sum(len(b) for _, b in out), a.pokefile))
    if a.patched:
        buf = bytearray(d)
        for off, b in out:
            buf[off:off + len(b)] = b
        assert len(buf) == len(d)
        Path(a.patched).write_bytes(bytes(buf))
        print('patched copy ->', a.patched)


if __name__ == '__main__':
    main()
