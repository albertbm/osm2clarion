# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write the Clarion ICODE area-name frame.

`REFER001/ICODE001.DAT` and `REFER002/ICODE001.DAT` are uncompressed and hold two
structures this module touches:

* the ISO table at `0x1000` - a 4-byte header, then one 462-byte entry per country
  carrying the country's ISO 3166 number and seven `0x40` tier-label slots. The number
  of non-empty slots is the country's area ladder, and an ADDRE or ZCODE record's tier
  vector has to be the same length.
* the area-name tree - one record chain per language, 42 country rows and a terminator,
  each row pointing at its own child chain. Rows are reached by index, so a street's
  `(country, level1, ...)` vector walks straight down it.

Offsets and sizes inside the tree are halfwords relative to the start of that
language's own country chain, so a chain can be moved without touching anything
outside it as long as its parent's offset and size move with it.

Everything here was measured against `ADDRE002`/`ICODE001` on a factory card: 96,472
of the 99,455 chains in one language table round-trip byte for byte through
`read_list` and `pack_list`, and the remainder differ only in how many terminators
they carry (see `pack_list`).
"""
import struct

ENTRY = 462            # one ISO table entry
SLOT = 0x40            # one tier label slot
NSLOT = 7
ISO_TABLE = 0x1000
TERM = 6               # a terminator record, in halfwords
NOSUB = 0x8000         # set in a row's size word when it has no child chain


class Row:
    """One area-name record: a name, a child-chain offset and that chain's size."""

    __slots__ = ('off', 'nosub', 'rlen', 'nlen', 'roff', 'cnt', 'idx', 'name', 'field')

    def __init__(self, off, nosub, rlen, nlen, roff, cnt, idx, field):
        self.off, self.nosub, self.rlen = off, nosub, rlen
        self.nlen, self.roff, self.cnt, self.idx = nlen, roff, cnt, idx
        self.field = field                  # the name field as stored, padding included
        self.name = field.rstrip(b'\0')

    def __repr__(self):
        return 'Row(%d, %r, roff=%d, cnt=%d)' % (self.idx, self.name, self.roff, self.cnt)


def read_row(d, off):
    rlen, nlen, roff, cnt, idx = struct.unpack_from('>HHIHH', d, off)
    field = bytes(d[off + 12:off + 12 + nlen * 2])
    return Row(off, rlen >> 15, rlen & 0x7fff, nlen, roff, cnt, idx, field)


def read_list(d, off):
    """Rows of the chain at `off`, stopping before the terminator(s)."""
    out = []
    while True:
        r = read_row(d, off)
        if r.nlen == 0:
            return out
        out.append(r)
        off += r.rlen * 2


def pack_row(name, roff, cnt, idx, leaf=False, width=None):
    """One record. A leaf carries offset ffffffff, size 0 and the no-sub bit.

    `width` pads the name field to that many halfwords, which is what a rename into an
    existing row needs: the field is fixed width and the row size must not change.
    """
    nm = name.encode('utf-8') if isinstance(name, str) else name
    nm += b'\0' * (-len(nm) % 2)
    if width is not None:
        assert len(nm) <= width * 2, (name, width)
        nm += b'\0' * (width * 2 - len(nm))
    if leaf:
        roff, cnt = 0xffffffff, 0
    return struct.pack('>HHIHH', (len(nm) // 2 + 6) | (NOSUB if leaf else 0),
                       len(nm) // 2, roff, cnt, idx) + nm


def pack_list(rows, parent_roff, deepest=False):
    """A whole chain plus its terminator(s). The parent's size field is `len(result)//2`.

    Every chain ends in `[6][0][ffffffff][parent chain's own offset]`; that back-link
    is exact on all 8,225 chains walked under Italy, Andorra, Monaco, San Marino and
    the Vatican. A chain of leaves that sits at the **deepest level its country's ISO
    entry declares** carries one more terminator ahead of that one, with the no-sub bit
    set and the row count in place of the link. Measured: every one of the 86,727
    fifth-level chains of a five-label country has it, 28 of the 40 second-level chains
    of a three-label country have it, and no chain above its country's deepest level
    does.
    """
    body = b''.join(rows)
    if deepest:
        body += struct.pack('>HHIHH', NOSUB | TERM, 0, 0xffffffff, 0, len(rows))
    return body + struct.pack('>HHII', TERM, 0, 0xffffffff, parent_roff)


def iso_entry(d, ordinal):
    """(ISO 3166 number, tier labels) for one country, from the ISO table at 0x1000."""
    o = ISO_TABLE + 4 + ordinal * ENTRY
    _stride, iso = struct.unpack_from('>HH', d, o)
    labels = []
    for s in range(NSLOT):
        t = bytes(d[o + 4 + s * SLOT:o + 4 + (s + 1) * SLOT]).split(b'\0')[0]
        if t:
            labels.append(t.decode('utf-8', 'replace'))
    return iso, labels


def pack_slot(label):
    """One tier-label slot. The factory writes a '2=' prefix on every one."""
    if label is None:
        return b'\0' * SLOT
    b = b'\0' * 4 + b'2=' + label.encode('utf-8')
    assert len(b) <= SLOT, label
    return b + b'\0' * (SLOT - len(b))


def iso_slots(labels):
    """All seven slots for one country. Slots past `labels` are zeroed, which is how the
    factory marks a shorter ladder - Andorra fills three of the seven."""
    assert len(labels) <= NSLOT, labels
    return b''.join(pack_slot(labels[s] if s < len(labels) else None) for s in range(NSLOT))


def iso_slots_off(ordinal):
    """Where one country's seven slots start, for a byte patch."""
    return ISO_TABLE + 4 + ordinal * ENTRY + 4
