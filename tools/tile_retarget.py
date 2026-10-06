#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rewrite a tile's header for another database version.

The 2017 and 2014 European tiles are byte-identical apart from six header bytes: the
UTF-16BE "DATA VERSION 17/09/01/01" string and the NAVTEQ quarter. Measured on all four
tiles of both cards: same size, same six offsets, nothing else. So one build covers both
databases, and this does the substitution. Control: run on the 2017 card's own four
tiles it reproduces the 2014 card's four tiles byte for byte.

    ./tile_retarget.py IN.DAT OUT.DAT [--to 2014]
"""
import sys

# offset -> (byte as built, byte wanted)
DATABASES = {
    '2014': {0x00fd: (0x37, 0x34), 0x0101: (0x30, 0x31), 0x0103: (0x39, 0x31),
             0x0107: (0x30, 0x31), 0x0109: (0x31, 0x37), 0x03c1: (0x37, 0x34)},
}
TEMPLATE = '2017'          # what the build writes, and so what every table substitutes from


def convert(d, to=TEMPLATE):
    """Tile bytes rewritten for one database. Asserts each byte is what the build wrote,
    so a tile already converted, or built from the wrong template, fails loudly."""
    if to == TEMPLATE:
        return bytes(d)
    b = bytearray(d)
    for off, (was, now) in DATABASES[to].items():
        assert b[off] == was, 'byte %#x is %#02x, not the %s value %#02x' % (off, b[off], TEMPLATE, was)
        b[off] = now
    return bytes(b)


def main():
    argv = sys.argv[1:]
    to = '2014'
    if '--to' in argv:
        i = argv.index('--to'); to = argv[i + 1]; del argv[i:i + 2]
    if to not in DATABASES and to != TEMPLATE:
        raise SystemExit('unknown database %r (have: %s)' % (to, ', '.join(DATABASES)))
    src, dest = argv[:2]
    out = convert(open(src, 'rb').read(), to)
    open(dest, 'wb').write(out)
    print('wrote %s for the %s database, %d bytes' % (dest, to, len(out)))


if __name__ == '__main__':
    main()
