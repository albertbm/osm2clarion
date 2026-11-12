# SPDX-License-Identifier: GPL-3.0-or-later
"""Write REFER001/ICODE001.DAT and REFER002/ICODE001.DAT from scratch: one country, Iceland.

The row and chain layout comes from icode_io. The country name, the tier labels and the
ISO code are Iceland's.

Each file is an authored container header, a one-entry ISO table at 0x1000 (ordinal 0,
ISO 352, three tier labels), the volume table at 0x5c00 where the slot table points, and
one area frame per language. REFER001's area tree is ISLAND, the towns under it, and each
town repeated once as its settlement. REFER002's tree is the factory shape for a country
with no REFER002 POINT file: one leaf row (no-sub bit, offset ffffffff, size 0), which is
how it ships Albania and Belarus.

REFER001 also carries a POI genre frame per language, the category and brand vocabulary
for POI search. POI is out of scope, so those 18 frames are copied byte for byte from the
card; they contain no country rows. Their chain offsets are relative to the frame start,
so they move with it.

REYKJAVIK is town row 0. Where-am-I resolves the town by index and nothing on the card is
known to carry a town index for a position, so row 0 is the one a zero index lands on.

    python3 tools/addr/build_icode_clean.py <card REFER001 ICODE001.DAT> <out dir>
"""
import argparse
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import icode_io  # noqa: E402
import addr_csv  # noqa: E402
import iceland_card  # noqa: E402
import refer_header  # noqa: E402

ISO = 352
LABELS = ('COUNTRY', 'SVEITARFÉLAG(MUNICIPALITY)', 'SETTLEMENT')
COUNTRY = 'ISLAND'
VOL = 0x5c00             # slot 13 points here on both factory files
VREC = 26
AREA_F20 = 0x180         # every area frame, 38 of 38 across both files
ENTRY_TAIL = b'\xff' * 10

TOWNS = addr_csv.area_towns()


def addr(off):
    assert off % 256 == 0
    return (off // 2048) << 8 | (off % 2048) // 256


def unaddr(a):
    return (a >> 8) * 2048 + (a & 0x3f) * 256


def iso_table():
    entry = struct.pack('>HH', icode_io.ENTRY // 2, ISO) + icode_io.iso_slots(list(LABELS)) + ENTRY_TAIL
    assert len(entry) == icode_io.ENTRY
    body = entry
    return struct.pack('>HH', (4 + len(body)) // 2, 1) + body


def align4(n):
    return (n + 3) & ~3


def area_tree(towns):
    """REFER001: ISLAND / town / town. Returns (frame bytes, top chain halfwords)."""
    top_len = len(icode_io.pack_list([icode_io.pack_row(COUNTRY, 0, 0, 0)], 0xffffffff))
    l1_at = align4(top_len)
    l1_len = len(icode_io.pack_list([icode_io.pack_row(t, 0, 0, i) for i, t in enumerate(towns)], 0))
    at = align4(l1_at + l1_len)
    leaves = []
    for t in towns:
        leaf = icode_io.pack_list([icode_io.pack_row(t, 0, 0, 0, leaf=True)], l1_at // 2, deepest=True)
        leaves.append((at, leaf))
        at = align4(at + len(leaf))
    l1 = icode_io.pack_list([icode_io.pack_row(t, a // 2, len(lf) // 2, i)
                             for i, (t, (a, lf)) in enumerate(zip(towns, leaves))], 0)
    assert len(l1) == l1_len
    top = icode_io.pack_list([icode_io.pack_row(COUNTRY, l1_at // 2, len(l1) // 2, 0)], 0xffffffff)
    buf = bytearray(at)
    buf[0:len(top)] = top
    buf[l1_at:l1_at + len(l1)] = l1
    for a, lf in leaves:
        buf[a:a + len(lf)] = lf
    return bytes(buf), len(top) // 2


def leaf_tree():
    """REFER002: one country row with no subtree."""
    top = icode_io.pack_list([icode_io.pack_row(COUNTRY, 0, 0, 0, leaf=True)], 0xffffffff)
    return top + b'\0' * (align4(len(top)) - len(top)), len(top) // 2


def max_rlen(frame):
    """The volume record's +20 field: the longest record in the frame, halfwords."""
    best = 0
    seen = set()
    stack = [0]
    while stack:
        off = stack.pop()
        if off in seen:
            continue
        seen.add(off)
        for r in icode_io.read_list(frame, off):
            best = max(best, r.rlen)
            if not r.nosub and r.cnt:
                stack.append(r.roff * 2)
    return best


def volume_records(d):
    out = []
    k = 0
    while True:
        p = VOL + VREC * k
        rec = struct.unpack_from('>HIIIHHBBHHH', d, p)
        out.append(rec)
        if rec[0] == 0xffff:
            return out
        k += 1


def build(src, poi=True, tree=area_tree):
    """src: the card's ICODE001.DAT, read for its volume table and POI frames only."""
    vol = volume_records(src)
    frames = []
    for nxt, sa, tot, z, flc, f20, lang, kind, f24, idx, z2 in vol:
        if kind == 0:
            if not poi:
                continue
            o = unaddr(sa)
            frame = src[o:o + tot * 2]
            assert max_rlen(frame) == f24, 'POI frame %d does not walk' % lang
            frames.append(dict(kind=0, lang=lang, data=frame, flc=flc, f20=f20, f24=f24))
        else:
            data, flc = tree(TOWNS) if tree is area_tree else tree()
            frames.append(dict(kind=1, lang=lang, data=data, flc=flc, f20=AREA_F20,
                               f24=max_rlen(data)))
    n = len(frames)
    vt = bytearray()
    at = VOL + ((VREC * n + 255) // 256) * 256
    for k, fr in enumerate(frames):
        fr['at'] = at
        at += (len(fr['data']) + 255) // 256 * 256
        vt += struct.pack('>HIIIHHBBHHH', 0xffff if k == n - 1 else VREC // 2 * (k + 1),
                          addr(fr['at']), len(fr['data']) // 2, 0, fr['flc'], fr['f20'],
                          fr['lang'], fr['kind'], fr['f24'], k, 0)
    out = bytearray(at)
    iso = iso_table()
    out[0:0x1000] = refer_header.header({4: (addr(0x1000), (len(iso) + 255) // 256),
                                         13: (addr(VOL), (len(vt) + 255) // 256)},
                                        iceland_card.BOUNDS, iceland_card.VERSION)
    out[0x1000:0x1000 + len(iso)] = iso
    out[VOL:VOL + len(vt)] = vt
    for fr in frames:
        out[fr['at']:fr['at'] + len(fr['data'])] = fr['data']
    return bytes(out), frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('icode1')
    ap.add_argument('icode2')
    ap.add_argument('outdir')
    a = ap.parse_args()
    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    r1, f1 = build(Path(a.icode1).read_bytes())
    r2, f2 = build(Path(a.icode2).read_bytes(), poi=False, tree=leaf_tree)
    (od / 'REFER001_ICODE001.DAT').write_bytes(r1)
    (od / 'REFER002_ICODE001.DAT').write_bytes(r2)
    print('REFER001 %d B, %d frames (%d area, %d POI)' % (
        len(r1), len(f1), sum(f['kind'] for f in f1), sum(1 - f['kind'] for f in f1)))
    print('REFER002 %d B, %d frames' % (len(r2), len(f2)))


if __name__ == '__main__':
    main()
