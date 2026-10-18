# SPDX-License-Identifier: GPL-3.0-or-later
"""Authored frame headers and definition regions for ADDRE, ZCODE and POINT, and an ADM writer.

The writers addre_io, zcode_io and point_io patch a donor's first 0x1100 bytes and
definition region in place. This module supplies those bytes from scratch instead.
Each template is the factory region with its per-file fields zeroed, and was checked
identical across factory files of unrelated countries before being written down here:

    ADDRE frame header, definition region   ADDRE002 Andorra, 040 Vatican, 042 Belarus,
                                            011 Greece, 007 Germany, 041 UK
    ZCODE frame header                      ZCODE002, 006, 012, 013, 016, 021, 029, 040
    POINT frame header, definition region   POINT002 Andorra, 016, 020, 029, 040 Vatican,
    ADM frame header, definition region     042 Belarus

Every zeroed field is written from this card's content by a formula measured exact on
those same files. `adm()` writes the ADM frame (the City list), laid out the way the six
factory files lay theirs out.
"""
import struct

import refer_header
from addre_io import ALIGN, addr, build_trie, deflate_block


def _t(size, runs):
    b = bytearray(size)
    for off, hx in runs.items():
        v = bytes.fromhex(hx)
        b[off:off + len(v)] = v
    return bytes(b)


ADDRE_FH = _t(0x100, {0x000: 'ffff', 0x004: '0201', 0x010: '4f', 0x01b: '0b', 0x030: 'ffff',
                      0x039: '02', 0x03c: 'ffff'})
ADDRE_DF = _t(0x100, {0x000: 'ffff0010ffffffff', 0x00c: 'ffffffff', 0x020: 'ffff003d',
                      0x039: '1200020600535452454554001f', 0x04a: '80',
                      0x052: '09006172656120636f646500ffff', 0x064: '60',
                      0x06b: '010b00737472656574206e616d65', 0x082: '01f4',
                      0x08c: '0600535452454554'})
ZCODE_FH = _t(0x100, {0x000: 'ffff', 0x004: '0201', 0x010: '4d80', 0x01b: '08', 0x030: 'ffff',
                      0x036: '08', 0x039: '025a49502d434f4445', 0x044: 'ffff'})
POINT_FH = _t(0x100, {0x000: 'ffff', 0x004: '0201', 0x010: '4c02', 0x01b: '14', 0x030: 'ffff',
                      0x036: '06', 0x039: '02524547494f4e', 0x042: 'ffff'})
POINT_DF = _t(0x200, {
    0x000: 'ffff0015ffffffff', 0x00c: 'ffffffff08', 0x016: '10', 0x019: '1020',
    0x01e: '0a0054454c2d4e554d424552008200a6', 0x043: '1300070800464143494c4954590020',
    0x056: '80', 0x05e: '09006172656120636f6465', 0x06b: '2c', 0x06f: '0180',
    0x077: '01080063617465676f7279003a', 0x087: '0180',
    0x08f: '020c007375622d63617465676f72790045', 0x0a4: '80', 0x0ab: '030500696e646578',
    0x0b5: '54', 0x0ba: '60', 0x0c1: '040d00666163696c697479206e616d65', 0x0d3: '60',
    0x0d8: '60', 0x0df: '0507006164647265737300ffff', 0x0ef: '0180',
    0x0f7: '06090069636f6e20636f6465', 0x105: 'b4',
    0x11b: '1400010b00494e464f524d4154494f4e00ffff', 0x132: '60',
    0x13a: '100074656c6570686f6e65206e756d626572', 0x15e: '0800464143494c495459',
    0x17a: '0b00494e464f524d4154494f4e'})
ADM_FH = _t(0x100, {0x000: 'ffff', 0x010: '4d', 0x01b: '09', 0x030: 'ffff', 0x039: '02',
                    0x03c: 'ffff'})
ADM_DF = _t(0x100, {0x000: 'ffff0010ffffffff', 0x00c: 'ffffffff', 0x020: 'ffff004a',
                    0x039: '110003030041444d', 0x043: '1e', 0x048: '80',
                    0x050: '09006172656120636f6465', 0x05d: '2d', 0x061: '0180',
                    0x069: '010d006c616e6775616765207479706500ffff', 0x080: '60',
                    0x087: '02090041726561206e616d65', 0x0a6: '030041444d'})

ADM_REC = 18
ADM_REC_W = 9
ADM_PER = 2000           # names per block, every factory ADM frame
ADM_CR, ADM_KW = 14, 2


def up(n):
    return n + (-n) % ALIGN


def addre_prefix(bounds, version):
    return refer_header.header({6: (addr(0x1000), 1), 7: (0, 1)}, bounds, version) + ADDRE_FH


def addre_hints(names):
    """D (+0x32) and the name-width byte (+0x63), both measured exact on six files: D is
    twice the longest reference-and-house-number run of any name record plus 2, and the
    byte is the longest name text in halfwords."""
    d = 2 * (max(n[2] + n[3] for n in names) + 1)
    w = 0
    for n in names:
        p = 8 + (n[2] * 2 - 4) + n[3] * 2
        nl = n[p]
        if nl:
            w = max(w, struct.unpack_from('<H', n, p + 1)[0])
    return d, w


def zcode_prefix(area, bounds, version):
    h = bytearray(refer_header.header({278: (addr(0x1000), 1)}, bounds, version) + ZCODE_FH)
    struct.pack_into('>H', h, 0x1042, area)
    return bytes(h)


def point_prefix(bounds, version):
    return refer_header.header({6: (addr(0x1000), 1), 7: (0, 2)}, bounds, version) + POINT_FH


def point_d(names):
    """+0x3c: the ADDRE formula plus 12, exact on all six POINT files."""
    return 2 * (max(n[10] + n[11] for n in names) + 1) + 12


def adm_name(name, levels, lang):
    nm = name.encode('utf-8')
    nm += b'\0' * (-len(nm) % 2)
    body = (bytes([len(levels), 1, len(nm) // 2, 0xff]) + struct.pack(f'>{len(levels)}H', *levels)
            + struct.pack('>H', lang) + nm)
    return struct.pack('>H', (2 + len(body)) // 2) + body


def adm_record(levels, name_idx, later_word):
    lv = tuple(levels) + (0xffff,) * (6 - len(levels))
    return struct.pack('>6HHHH', *lv, 0x4000 if later_word else 0, 0, name_idx)


def adm(at, area, nlevel, names, keys):
    """The ADM frame, starting at file offset `at` (256-aligned).

    names: [(display name, ladder, language)] in file order.
    keys:  {key bytes (kw=2 units): [(name index, later word)]}.
    Returns (bytes, header offset, definition-region offset).
    """
    assert at % ALIGN == 0
    recs, tkeys = [], []
    for k in sorted(keys):
        hits = sorted(keys[k], key=lambda h: (h[1], h[0]))   # word starts first, as the factory
        tkeys.append((k, len(recs), len(hits)))
        for i, later in hits:
            recs.append(adm_record(names[i][1], i, later))
    node, first, _ = build_trie(tkeys, ADM_CR, ADM_KW, ADM_REC_W)
    nrec = [adm_name(*n) for n in names]

    h = bytearray(ADM_FH)
    struct.pack_into('>I', h, 0x02, addr(at + 0x100))
    struct.pack_into('>I', h, 0x06, len(node) // 2)
    struct.pack_into('>H', h, 0x0e, first)
    h[0x32] = ADM_CR // 2
    h[0x33] = ADM_KW
    struct.pack_into('>H', h, 0x3a, area)
    out = bytearray(h) + node
    rb = up(at + len(out))
    struct.pack_into('>I', out, 0x12, addr(rb))
    struct.pack_into('>I', out, 0x16, len(recs) * ADM_REC_W)
    out += b'\0' * (rb - at - len(out))
    out += b''.join(recs)
    dfo = up(at + len(out))
    out += b'\0' * (dfo - at - len(out))

    blocks, parts, st = [], [], 0
    for i in range(0, len(nrec), ADM_PER):
        plain = b''.join(nrec[i:i + ADM_PER])
        c = deflate_block(plain)
        c += b'\0' * (-len(c) % 4)
        blocks.append((st, len(c) // 2, len(plain) // 2))
        parts.append(c)
        st += len(c) // 2
    A = dfo + 0x100
    P = up(A + sum(len(p) for p in parts))
    df = bytearray(ADM_DF)
    struct.pack_into('>I', df, 0x24, addr(A))
    struct.pack_into('>I', df, 0x28, st)
    struct.pack_into('>H', df, 0x30, max(len(n) for n in nrec) // 2)
    df[0x47] = nlevel
    df[0x7f] = max(n[4] for n in nrec)
    struct.pack_into('>I', df, 0x94, addr(P))
    struct.pack_into('>I', df, 0x98, (4 + 12 * len(blocks)) // 2)
    struct.pack_into('>H', df, 0x9c, ADM_PER)
    out += df
    out += b''.join(parts)
    out += b'\0' * (P - at - len(out))
    t = bytearray(up(4 + 12 * len(blocks) + 4))
    struct.pack_into('>HH', t, 0, ADM_PER, 0)
    for k, b in enumerate(blocks):
        struct.pack_into('>III', t, 4 + 12 * k, *b)
    out += t
    return bytes(out), at, dfo
