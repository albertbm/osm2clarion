# SPDX-License-Identifier: GPL-3.0-or-later
"""The two map formats the tools write, and the values that differ between them.

X08-03-00 is the ZENRIN database on QY8 units, X08-02-00 the HITACHI one on QY7 units.
The container, grid and record layouts are shared. What differs is measured on the 2014
QY7 card (QSD-357-101) against the 2014 and 2017 QY8 European cards: a few header strings
and constants, the fill around the level and box tables, the length of the section table,
the section 2 level types, the zlib window and padding of a record, and how a directory
stores its mesh x, Latin-1 labels against UTF-8, and word 19 of a level-1 road-shape record
(0 on all 10,508 of B60R048R's, 0x8104 on QY8). A section 2 record's class-group table
counts a node under its class in both, but a link end under its node's class in QY8 (983 of
983 on B68R058R) and under its link's class in QY7 (3,001 of 3,001 on B60R048R and B58R050R).
A QY7 section 2 level 2 record is QY8's two parts, each its own zlib record in pairs 2c and
2c+1 of a 512-byte slot (all 61 columns on B68R058R).

A tile names its own format in its FORMAT VERSION string, so a reader takes it from the
header; a writer takes it from the profile's `format`.
"""
import zlib

FORMAT_OFF = 0xa0


class Format:
    def __init__(self, name, unit, ident, consts, zero, table_fill, sect_slots, levels, zmagic, wbits, pad, x_base,
                 codec, shape_w19, ends_by_link):
        self.name, self.unit = name, unit
        self.ident = ident              # UTF-16BE strings by offset
        self.consts = consts            # raw hex by offset, outside the 0xff fill
        self.zero = zero                # (start, end) ranges written as zero
        self.table_fill = table_fill    # fill byte after the level and box tables
        self.sect_slots = sect_slots    # length of the section table at 0x800
        self.levels = levels            # section id -> [(level, type)], in factory order
        self.zmagic = zmagic            # the two bytes a compressed record starts with
        self.wbits = wbits
        self.pad = pad                  # a compressed record is zero-padded to this many bytes
        self.x_base = x_base            # directories and section tables store mesh x minus this
        self.codec = codec              # label text
        self.shape_w19 = shape_w19      # word 19 of a level-1 road-shape record
        self.ends_by_link = ends_by_link  # graph header counts a link end under its link's class, not its node's

    def text(self, s):
        """Label bytes. A letter outside Latin-1 loses its accent rather than the label."""
        try:
            return s.encode(self.codec)
        except UnicodeEncodeError:
            import unicodedata
            return ''.join(c if ord(c) < 256 else unicodedata.normalize('NFKD', c)[0] for c in s).encode(self.codec, 'replace')

    def is_zlib(self, raw):
        return raw[:2] == self.zmagic

    def deflate(self, plain):
        """A compressed record as the factory writes it, padding included."""
        c = zlib.compressobj(6, zlib.DEFLATED, self.wbits, 8)
        z = c.compress(plain) + c.flush()
        return z + bytes(-len(z) % self.pad)

    def inflate(self, raw):
        """A record's plain bytes; an uncompressed record comes back as it is."""
        return zlib.decompress(raw) if self.is_zlib(raw) else raw

    def __repr__(self):
        return '<Format %s (%s)>' % (self.name, self.unit)


_SEC1 = [(6, 0x80), (5, 0x90), (4, 0x91), (3, 0x91), (2, 0x91), (1, 0x92)]
_SEC4 = [(1, 0x93)]

X08_03 = Format(
    'x08-03', 'QY8',
    ident={0x00: 'CLARION', 0x40: 'ZENRIN', 0x80: 'CLA-NAVI06-01',
           0xa0: 'FORMAT VERSION X08-03-00', 0x162: 'V06.00.00'},
    consts={0x160: '1600', 0x182: '24ff', 0x18c: '3f' * 20, 0x3ac: '0100',
            0x4ed: '01011000000120', 0x5cc: '80000000', 0x5d4: '0b0b'},
    zero=((0x4ac, 0x4ed), (0x52c, 0x5cc), (0x5d6, 0x800), (0x800 + 6 * 300, 0x1000)),
    table_fill=0xff, sect_slots=300,
    levels={1: _SEC1, 2: [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)], 4: _SEC4},
    zmagic=b'\x58\x85', wbits=13, pad=2, x_base=0, codec='utf-8', shape_w19=0x8104, ends_by_link=False)

# All 46 tile and LEV headers on the QY7 card rebuild from these.
X08_02 = Format(
    'x08-02', 'QY7',
    ident={0x00: 'CLARION', 0x40: 'HITACHI', 0x80: 'CLA-NAVI05-01',
           0xa0: 'FORMAT VERSION X08-02-00', 0x162: 'V05.00.00'},
    consts={0x160: '1600', 0x182: '24ff', 0x18c: '3f' * 20, 0x3ac: '0000',
            0x5cc: '80000000', 0x5d4: '0b0b'},
    zero=((0x4ac, 0x5cc), (0x5d6, 0x800), (0x800 + 6 * 341, 0x1000)),
    table_fill=0x00, sect_slots=341,
    levels={1: _SEC1, 2: [(5, 0x91), (4, 0x91), (2, 0x91), (1, 0x90)], 4: _SEC4},
    zmagic=b'\x78\x9c', wbits=15, pad=4, x_base=1024, codec='latin-1', shape_w19=0, ends_by_link=True)

FORMATS = {f.name: f for f in (X08_03, X08_02)}
DEFAULT = X08_03


def get(name):
    if name not in FORMATS:
        raise SystemExit('unknown map format %r (known: %s)' % (name, ', '.join(FORMATS)))
    return FORMATS[name]


def of_header(h):
    """The format a tile header names. A header without a known FORMAT VERSION string,
    such as one built in a test, is read as X08-03."""
    s = bytes(h[FORMAT_OFF:FORMAT_OFF + 48])
    for f in FORMATS.values():
        if s == f.ident[FORMAT_OFF].encode('utf-16-be'):
            return f
    return DEFAULT


def slot_body(section, level_type):
    """Bytes per directory slot. Section 1 uses 512; so does an X08-02 section 2 level of
    type 0x91, which X08-03 types 0x81 and gives 256."""
    return 512 if section == 1 or level_type == 0x91 else 256
