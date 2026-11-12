#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add one facility (a POINT category) to an already-built card's POINT001 file.

Proof of concept only: one facility at a hand-picked position, name and street. On a
stock EUR ZE1 card a dealer is a POINT record with category 43 and no sub-category, and
none of the five French dealers checked has a section-1 layer-25 point under it, so the
POINT record is the whole patch. The file's towns, keys and telephone frame are kept; the
ADM frame holds absolute addresses, so it is rebuilt where it lands.

    point_add.py IN.cmp IN.ind OUT_DIR LAT LON --category 43 --name NAME --street STREET
                 [--house N] [--town TOWN] [--addre ADDRE.cmp ADDRE.ind] [--tel NUMBER]
                 [--data-id N]

With --addre the street's ADDRE record supplies the parcel and the KANREN/INFO references
into the level-1 shape, as a factory's own facility records carry (Andorra's GARATGE
NOGREDA: refs 4/5 against the digitising direction, a house-number item, a telephone
record). --data-id sets the data record's 32-bit id (words 16-17), which the factory fills
with a database-wide facility id; without it the all-ones invalid is written.

Writes OUT_DIR/POINT001.DAT, .cmp and .ind. Put the pair on the card with fatedit.py.
"""
import argparse
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'addr'))
import addre_io  # noqa: E402
import point_io  # noqa: E402
import frames  # noqa: E402
from build_addr_area0 import AREA, LANG_OTHER, NLEVEL, REF_BASE, ladder, point_word_keys  # noqa: E402
from card_pack import wrap  # noqa: E402


def inflate(cmp, ind):
    offs = [int.from_bytes(ind[i:i + 4], 'little') for i in range(0x10, len(ind), 4)]
    return b''.join(zlib.decompress(cmp[st:(offs[i + 1] if i + 1 < len(offs) else len(cmp))])
                    for i, st in enumerate(offs))


def name_fields(nr):
    """(name, ladder, category) of one name record."""
    q = 16 + (nr[10] * 2 - 4) + nr[11] * 2 + 4   # past refs, items and the tel pair
    nlevel, nmlen = nr[q], nr[q + 4]
    levels = struct.unpack_from('>%dH' % nlevel, nr, q + 8)
    cat = struct.unpack_from('>H', nr, q + 8 + 2 * nlevel)[0]
    o = q + 12 + 2 * nlevel
    return nr[o:o + 2 * nmlen].rstrip(b'\0').decode('utf-8'), levels, cat


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src_cmp')
    ap.add_argument('src_ind')
    ap.add_argument('outdir')
    ap.add_argument('lat', type=float)
    ap.add_argument('lon', type=float)
    ap.add_argument('--category', type=int, required=True, help='POINT category, e.g. 43 for a car dealer')
    ap.add_argument('--name', required=True)
    ap.add_argument('--street', required=True)
    ap.add_argument('--house', default='2')
    ap.add_argument('--town', required=True)
    ap.add_argument('--addre', nargs=2, metavar=('CMP', 'IND'))
    ap.add_argument('--tel')
    ap.add_argument('--data-id', default='0xffffffff')
    a = ap.parse_args(argv)
    data_id = int(a.data_id, 0)

    plain = inflate(open(a.src_cmp, 'rb').read(), open(a.src_ind, 'rb').read())
    p = point_io.Point(plain)
    assert p.area == AREA and p.nlevel == NLEVEL, (p.area, p.nlevel)

    # the existing trie, as key -> [(name index, flags)]
    hits = {}
    for k, start, n in p.keys:
        for i in range(start, start + n):
            w = struct.unpack('>20H', p.rec(i))
            hits.setdefault(k, []).append((w[15], w[6]))

    lw, ow, iy, ix = addre_io.parcel_point(a.lat, a.lon)
    idx = len(p.names)
    ref = REF_BASE + 4 * idx
    refs = ((2, 0x40, ref), (3, 0x40, ref + 1))
    items = b''
    if a.addre:
        A = addre_io.Addre(inflate(open(a.addre[0], 'rb').read(), open(a.addre[1], 'rb').read()))
        want = a.street.encode('utf-8')
        rec = next(n for n in A.names if n.rstrip(b'\0').endswith(want))
        sla, slo = struct.unpack_from('>HH', rec, 4)
        nref = (rec[2] * 2 - 4) // 4
        refs = tuple(struct.unpack_from('>BBH', rec, 8 + 4 * i) for i in range(nref))
        assert (sla, slo) == (lw, ow), ('street shape is in another parcel', hex(sla), hex(slo), hex(lw), hex(ow))
        house = int(a.house)
        items = addre_io.house_item(house, house, addre_io.SIDE_EVEN if house % 2 == 0 else addre_io.SIDE_ODD)
    tels = list(p.tels)
    tel_idx = None
    if a.tel:
        tel_idx = len(tels)
        tels.append(point_io.tel_record(a.tel))
    names = list(p.names)
    names.append(point_io.name_record(
        a.name, ladder(a.town), NLEVEL, a.category, lat=lw, lon=ow, x=ix, y=iy,
        address=a.street, house=a.house, items=items, refs=refs, tel_idx=tel_idx))
    for u, later in point_word_keys(a.name, p.kw):
        hits.setdefault(u, []).append((idx, 0x4000 if later else 0))

    keys, recs = [], []
    for u in sorted(hits):
        hs = sorted(hits[u], key=lambda h: (h[1], h[0]))
        keys.append((u, len(recs), len(hs)))
        for i, fl in hs:
            nr = names[i]
            # the name record repeats ladder, category and position; read them back so
            # the data record of an old town stays what it was
            _t, levels, cat = name_fields(nr)
            la, lo, ila, ilo = struct.unpack_from('>HHHH', nr, 2)
            recs.append(point_io.data_record(levels, len(levels), i, cat, lat=la, lon=lo,
                                             x=ila & 0x7ff, y=ilo, flags=fl,
                                             data_id=data_id if i == idx else 0xffffffff))

    # the ADM frame (slots 280 and 287) holds absolute addresses, so it is rebuilt where
    # it lands rather than carried; first prove the rebuild reproduces the old one
    adm_names = []
    for nr in p.names:
        t, levels = name_fields(nr)[:2]
        adm_names += [(t, levels[:2], LANG_OTHER), (t, levels, LANG_OTHER)]
    adm_keys = {}
    for i, (nm, lv, lang) in enumerate(adm_names):
        for u, later in point_word_keys(nm, frames.ADM_KW):
            adm_keys.setdefault(u, []).append((i, later))
    old_frame = frames.adm(p.extra_off, p.area, p.nlevel, adm_names, adm_keys)[0]
    assert old_frame == p.extra, 'ADM rebuild differs from the file'

    out, nb, tb = point_io.assemble(p.prefix, p.df, keys, recs, names, tels, p.cr, p.kw,
                                    p.area, p.nlevel)
    at = frames.up(len(out))
    frame, h, dfo = frames.adm(at, p.area, p.nlevel, adm_names, adm_keys)
    out = bytearray(out) + b'\0' * (at - len(out)) + frame
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
    # the category search frames: one category, this record, its parcel
    cat_keys, cat_recs = [], []
    for u, start, n in keys:
        mine = [r for r in recs[start:start + n] if r[30:32] == struct.pack('>H', idx)]
        if mine:
            cat_keys.append((u, len(cat_recs), len(mine))); cat_recs += mine
    at = frames.up(len(out))
    blob, s15, s278 = point_io.category_frames(at, p.area, [(a.category, cat_keys, cat_recs)], names)
    out += b'\0' * (at - len(out)) + blob
    struct.pack_into('>IH', out, 0x800 + 6 * 15, *s15)
    struct.pack_into('>IH', out, 0x800 + 6 * 278, *s278)
    out = bytes(out)
    q = point_io.Point(out)
    assert len(q.names) == len(p.names) + 1 and q.nrec == p.nrec + len(point_word_keys(a.name, p.kw))

    os.makedirs(a.outdir, exist_ok=True)
    open(os.path.join(a.outdir, 'POINT001.DAT'), 'wb').write(out)
    c, ind = wrap(out)
    open(os.path.join(a.outdir, 'POINT001.DAT.cmp'), 'wb').write(c)
    open(os.path.join(a.outdir, 'POINT001.DAT.ind'), 'wb').write(ind)
    print('%s at parcel %04x %04x xy %d,%d: records %d -> %d, names %d -> %d, %d -> %d bytes'
          % (a.name, lw, ow, ix, iy, p.nrec, q.nrec, len(p.names), len(q.names), len(plain), len(out)))
    print('name record:', names[-1].hex())
    print('data record:', q.rec([i for i, r in enumerate(recs) if r[30:32] == struct.pack('>H', idx)][0]).hex())


if __name__ == '__main__':
    main(sys.argv[1:])
