# SPDX-License-Identifier: GPL-3.0-or-later
"""Author the 4 KB container header of a REFER file (ICODE, ADDRE, ZCODE, POINT, COORD).

Every byte here is either a format constant or a field this card decides. The constants
were measured identical across factory files of six unrelated countries - ADDRE002
Andorra, ADDRE040 Vatican, ADDRE042 Belarus, ADDRE011 Greece, ADDRE007 Germany, ADDRE041
UK - and across both ICODE001 files, which hold every country at once. `check()` rebuilds
a factory header from its own decoded fields and asserts it matches byte for byte.

Decided per file: the db bounds at 0x184 and 0x5d0 (arc-minutes; the factory writes the
whole database's extent), the slot table at 0x800 (sector address and 256-byte block
count per frame), and DATA VERSION, which has to be the target card's own vintage. The
NAVTEQ supplier string at 0x3b0 is left as 0xff fill unless a factory header is being
reproduced.
"""
import struct

FILL = 0xff
NSLOT = 341
SLOT0 = 0x800
ZERO_SLOTS = range(300, NSLOT)     # (0, 0) rather than (ffffffff, 0) on every factory file


def _s(text):
    return text.encode('utf-16-be')


CONSTS = [
    (0x000, _s('CLARION')),
    (0x040, _s('ZENRIN')),
    (0x080, _s('CLA-NAVI06-01')),
    (0x0a0, _s('FORMAT VERSION X08-03-00')),
    (0x160, bytes.fromhex('1400') + _s('V06.00.00')),
    (0x182, b'\x24'),
    # the 19 card languages in volume-table order, then 0x3f for the country's own
    (0x18c, bytes.fromhex('050402070a0608090b0c0d15161718191a1e143f')),
    (0x1ac, b'\0\0'),               # level table: no sections
    (0x2ac, b'\0\0'),               # box table: no sections
    (0x3ac, bytes.fromhex('0100')),
    (0x4ac, b'\0' * 66),
    (0x52c, b'\0' * 160 + bytes.fromhex('80000000')),
    (0x5d4, bytes.fromhex('0b0b') + b'\0' * 554),
]


def header(slots, bounds, version, navteq=None):
    """slots: {slot number: (sector address, 256-byte block count)}. bounds: lat max, lat
    min, lon min, lon max in arc-minutes. version: the card's DATA VERSION, '14/11/17/01'."""
    h = bytearray([FILL]) * 0x1000
    for off, b in CONSTS:
        h[off:off + len(b)] = b
    v = _s('DATA VERSION ' + version)
    h[0x0e0:0x0e0 + len(v)] = v
    struct.pack_into('>4h', h, 0x184, *bounds)
    struct.pack_into('>2h', h, 0x5d0, bounds[1], bounds[2])
    if navteq is not None:                       # only for reproducing a factory header
        h[0x3b0:0x3b0 + len(navteq)] = navteq
    for k in range(NSLOT):
        a, c = slots.get(k, (0, 0) if k in ZERO_SLOTS else (0xffffffff, 0))
        struct.pack_into('>IH', h, SLOT0 + 6 * k, a, c)
    h[SLOT0 + 6 * NSLOT:] = b'\0' * (0x1000 - SLOT0 - 6 * NSLOT)
    return bytes(h)


def slots_of(h):
    out = {}
    for k in range(NSLOT):
        a, c = struct.unpack_from('>IH', h, SLOT0 + 6 * k)
        if (a, c) != ((0, 0) if k in ZERO_SLOTS else (0xffffffff, 0)):
            out[k] = (a, c)
    return out


def check(h):
    """Rebuild a factory header from its own bounds, slots, version and NAVTEQ string."""
    bounds = struct.unpack_from('>4h', h, 0x184)
    version = bytes(h[0x0fa:0x0fa + 22]).decode('utf-16-be')
    nav = h[0x3b0:0x3b0 + 22]
    return header(slots_of(h), bounds, version, nav) == bytes(h[:0x1000])
