# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the Clarion ZCODE postcode-search family (decompressed form).

Same letter trie as ADDRE, digits for keys, 16-byte records and no name block.
"""
import struct

from addre_io import ALIGN, build_trie

ZREC = 16
ZREC_W = 8


def assemble(prefix, codes, area, tiers=(0, 0, 0), cr=12, kw=1):
    """codes: sorted list of postcode strings. One record each."""
    keys = [(c.encode('ascii'), i, 1) for i, c in enumerate(sorted(codes))]
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
    body = struct.pack('>H', area) + struct.pack(f'>{len(tiers)}H', *tiers)
    rec = body + b'\xff' * (ZREC - 2 - len(body)) + b'\x00\x00'
    assert len(rec) == ZREC, len(rec)
    out += rec * len(keys)
    out += b'\x00' * ((-len(out)) % ALIGN)
    return bytes(out)
