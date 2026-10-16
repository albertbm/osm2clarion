# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the Clarion ZCODE postcode-search family (decompressed form).

Same letter trie as ADDRE, digits for keys, 16-byte records and no name block.
"""
import struct

from addre_io import ALIGN, addr, build_trie

ZREC = 16
ZREC_W = 8


def assemble(prefix, codes, levels, cr=12, kw=1):
    """codes: sorted postcode strings. `levels` maps each code to its area ladder.

    One 16-byte record per code: the ladder as big-endian halfwords, country first,
    0xff for a level the code does not reach, then two zero bytes. Measured on
    `ZCODE002`, where each of Andorra's seven codes carries (country, parish) and
    leaves the settlement 0xffff, and on `ZCODE013`, where Italy's 12,900 carry up to
    all five. So this is what ties a postcode to a town.
    """
    codes = sorted(codes)
    keys = [(c.encode('ascii'), i, 1) for i, c in enumerate(codes)]
    node, first, _nroot = build_trie(keys, cr, kw, ZREC_W)
    out = bytearray(prefix)
    out[0x1032] = cr // 2
    out[0x1033] = kw
    struct.pack_into('>I', out, 0x1006, len(node) // 2)
    struct.pack_into('>H', out, 0x100e, first)
    struct.pack_into('>I', out, 0x1016, len(keys) * ZREC_W)
    struct.pack_into('>H', out, 0x101a, ZREC_W)
    out += node
    out += b'\x00' * ((-len(out)) % ALIGN)
    # 0x1012 is the record block's own sector address. It is exact on all 36 factory
    # ZCODE files and the donor prefix carries the donor's value, which points past
    # the end of a smaller file.
    struct.pack_into('>I', out, 0x1012, addr(len(out)))
    for c in codes:
        lv = levels[c]
        body = struct.pack('>%dH' % len(lv), *lv)
        rec = body + b'\xff' * (ZREC - 2 - len(body)) + b'\x00\x00'
        assert len(rec) == ZREC, (c, len(rec))
        out += rec
    out += b'\x00' * ((-len(out)) % ALIGN)
    return bytes(out)
