#!/usr/bin/env python3
"""Turn a 2017-template Iceland tile into its 2014 DKL twin.

The two vintages' tiles are byte-identical apart from six bytes of the header: the UTF-16BE
"DATA VERSION 17/09/01/01" string and the NAVTEQ quarter. Measured on the four tiles of both
cards, 6 Oct 2026 -- same size, same six offsets, nothing else. Control: run on the ZE1 card's
own tiles it reproduces the DKL card's tiles byte for byte.

    tile_to_dkl.py IN.DAT OUT.DAT
"""
import sys
SUBS = {0x00fd: (0x37, 0x34), 0x0101: (0x30, 0x31), 0x0103: (0x39, 0x31),
        0x0107: (0x30, 0x31), 0x0109: (0x31, 0x37), 0x03c1: (0x37, 0x34)}

def convert(d):
    b = bytearray(d)
    for off, (ze1, dkl) in SUBS.items():
        assert b[off] == ze1, 'byte %#x is %#02x, not the 2017 value %#02x' % (off, b[off], ze1)
        b[off] = dkl
    return bytes(b)

if __name__ == '__main__':
    open(sys.argv[2], 'wb').write(convert(open(sys.argv[1], 'rb').read()))
