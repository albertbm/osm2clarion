# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write INTER<nnn>.DAT, the Clarion intersection search (X08-02, QY7).

Measured on INTER007 (Finland) of the 2014 QY7 card. The file has ADDRE's shape, with
POINT's two-block definition region and two name frames:

* the trie (cr 12, kw 1) over the first street's folded name, one key per word start,
  as ADDRE cuts them; a record index counts 9-halfword rows.
* the rows, packed as an X08-02 ADDRE's are: 0x4e20, a zero word, one u32 size in
  halfwords per 20,000 rows, one zlib stream each. A row is the first street's ladder
  (5 halfwords), ffff, 0x0000 for a whole-name hit or 0x4000 for a later word, and the
  index of its 1ST-STREET record (u32).
* 1ST-STREET: one record per (name, ladder), sorted by Latin-1 name then ladder:
  [flags 0x14 | words][u32 first 2ND-STREET record][u16 count][levels][name words][ladder][name].
* 2ND-STREET: each first street's crossing streets, one record per (name, ladder) sorted
  the same way, flags 0x10 on the first of a run and 0x11 after it:
  [flags | words][LA][LO][x][y][levels][name words][ladder][name]. LA, LO are the level-1
  parcel code and x, y the junction inside it in 2048ths, the unit of a drawn-shape point.
* two tail tables, 1500 and 2200 records per name block.
"""
import re
import struct
import zlib

from addre_io import ALIGN, V5_PER, addr, build_trie, fold_key, unaddr
from point_io import _read_frame

ROW = 18
ROW_W = 9
DF_LEN = 0x200
PER1, PER2 = 1500, 2200
CR, KW = 12, 1
FIRST_FLAGS = 0x14
SECOND_FIRST, SECOND_MORE = 0x10, 0x11
LATER = 0x4000

# definition-region offsets: (frame address, compressed words, longest record words,
# ladder levels, longest name words); the last two exact on nine countries' files
DF_N1 = (0x24, 0x28, 0x30, 0x4d, 0x67)
DF_N2 = (0x7e, 0x82, 0x8a, 0xa7, 0xc1)
DF_T1, DF_T2 = 0xd6, 0xf4      # tail address, tail words, records per block


def u16(d, o): return struct.unpack_from('>H', d, o)[0]
def u32(d, o): return struct.unpack_from('>I', d, o)[0]


def trie_keys(d, base=0x1100):
    """[(key, first row, count)]; point_io.trie_keys with the root ended by its all-ones key,
    since a root that opens on a leaf at row 0 has a zero first word (INTER017, INTER032)."""
    def crec(o):
        return u32(d, o), u16(d, o + 4), d[o + 8:o + 8 + KW], int.from_bytes(d[o + 8 + KW:o + CR], 'big')
    o, root = base, []
    while d[o + 8:o + 8 + KW] != b'\xff' * KW:
        root.append(crec(o))
        o += CR
    keys, stack = [], [(c, b'') for c in root]
    while stack:
        c, pfx = stack.pop()
        k = pfx + c[2]
        if c[1] == 0:
            keys.append((k, c[0] // ROW_W, c[3]))
            continue
        fo = base + c[0] * 2
        n = c[1] * 2 // CR - 1
        own = int.from_bytes(d[fo + n * CR + 8 + KW:fo + (n + 1) * CR], 'big')
        if own:
            keys.append((k, u32(d, fo + n * CR) // ROW_W, own))
        for i in range(n):
            stack.append((crec(fo + i * CR), k))
    return sorted(keys)


class Inter:
    """Parsed file: prefix (0..0x1100), keys, rows, df, first and second (name records)."""

    def __init__(self, d):
        self.d = d
        assert d[0x1032] * 2 == CR and d[0x1033] == KW, (d[0x1032], d[0x1033])
        self.prefix = d[:0x1100]
        self.area = u16(d, 0x103a)
        self.nrow = u32(d, 0x1016) // ROW_W
        o = unaddr(u32(d, 0x1012))
        assert u16(d, o) == V5_PER, hex(u16(d, o))
        n = -(-self.nrow // V5_PER)
        p = o + 4 + 4 * n
        rows = b''
        for i in range(n):
            size = u32(d, o + 4 + 4 * i) * 2
            rows += zlib.decompress(d[p:p + size])
            p += size
        assert len(rows) == self.nrow * ROW, (len(rows), self.nrow)
        self.rows = [rows[i:i + ROW] for i in range(0, len(rows), ROW)]
        dfr = unaddr(u32(d, 0x800 + 6 * 7))
        self.df = d[dfr:dfr + DF_LEN]
        self.first, _, self.per1 = _read_frame(d, unaddr(u32(self.df, DF_N1[0])), unaddr(u32(self.df, DF_T1)))
        self.second, _, self.per2 = _read_frame(d, unaddr(u32(self.df, DF_N2[0])), unaddr(u32(self.df, DF_T2)))
        self.keys = trie_keys(d)


def _pack(recs, per, deflate):
    """(compressed bytes, [(start, compressed words, plain words)]) of one name frame."""
    parts, blocks, st = [], [], 0
    for i in range(0, len(recs), per):
        plain = b''.join(recs[i:i + per])
        c = deflate(plain)
        c += b'\0' * (-len(c) % 4)
        blocks.append((st, len(c) // 2, len(plain) // 2))
        parts.append(c)
        st += len(c) // 2
    return b''.join(parts), blocks


def _tail(blocks, per):
    t = bytearray(4 + 12 * len(blocks) + 4)
    struct.pack_into('>HH', t, 0, per, 0)
    for i, b in enumerate(blocks):
        struct.pack_into('>III', t, 4 + 12 * i, *b)
    return bytes(t)


def assemble(prefix, df, keys, rows, first, second, area, deflate, per1=PER1, per2=PER2):
    """Bytes of an INTER file; prefix and df are templates, every size and address is set here.
    deflate is the card format's (clarion_format.X08_02.deflate)."""
    node, first_word, _ = build_trie(keys, CR, KW, ROW_W)
    out = bytearray(prefix)
    struct.pack_into('>I', out, 0x1006, len(node) // 2)
    struct.pack_into('>H', out, 0x100e, first_word)
    struct.pack_into('>I', out, 0x1016, len(rows) * ROW_W)
    struct.pack_into('>H', out, 0x103a, area)
    out += node
    out += b'\0' * (-len(out) % ALIGN)
    struct.pack_into('>I', out, 0x1012, addr(len(out)))
    plain = b''.join(rows)
    assert len(plain) == len(rows) * ROW
    parts = [deflate(plain[i:i + V5_PER * ROW]) for i in range(0, len(plain), V5_PER * ROW)]
    out += struct.pack('>HH', V5_PER, 0) + b''.join(struct.pack('>I', len(c) // 2) for c in parts)
    out += b''.join(parts)
    out += b'\0' * (-len(out) % ALIGN)
    dfr = len(out)
    struct.pack_into('>I', out, 0x824 + 6, addr(dfr))      # slot 7
    d = bytearray(df)
    assert len(d) == DF_LEN
    out += d
    frames = []
    for recs, per, (fa, fw, fm, fl, fn), nw in ((first, per1, DF_N1, 9), (second, per2, DF_N2, 11)):
        cmp_, blocks = _pack(recs, per, deflate)
        at = len(out)
        out += cmp_
        out += b'\0' * (-len(out) % ALIGN)
        struct.pack_into('>I', out, dfr + fa, addr(at))
        struct.pack_into('>I', out, dfr + fw, sum(b[1] for b in blocks))
        struct.pack_into('>H', out, dfr + fm, max(len(r) for r in recs) // 2)
        out[dfr + fl] = recs[0][nw - 1]
        out[dfr + fn] = max(r[nw] for r in recs)
        frames.append((blocks, per))
    for (blocks, per), ta in zip(frames, (DF_T1, DF_T2)):
        t = _tail(blocks, per)
        struct.pack_into('>IIH', out, dfr + ta, addr(len(out)), len(t) // 2 - 2, per)
        out += t
        out += b'\0' * (-len(out) % ALIGN)
    return bytes(out)


def row(levels, first_idx, later=False):
    """One row; a ladder shorter than five levels is padded with ffff."""
    lv = tuple(levels) + (0xffff,) * (5 - len(levels))
    return struct.pack('>5HHHI', *lv, 0xffff, LATER if later else 0, first_idx)


def parse_row(r):
    w = struct.unpack('>5HHHI', r)
    return dict(levels=w[:5], later=w[6] == LATER, first_idx=w[7])


def _name(text, levels):
    nm = text + b'\0' * (len(text) % 2)
    return bytes([len(levels), len(nm) // 2]) + struct.pack('>%dH' % len(levels), *levels) + nm


def first_record(name, levels, second_at, count, codec='latin-1'):
    """One 1ST-STREET record; name is bytes as stored, or a str encoded with codec."""
    nm = name.encode(codec) if isinstance(name, str) else name
    body = struct.pack('>IH', second_at, count) + _name(nm, levels)
    return struct.pack('>H', FIRST_FLAGS << 11 | (2 + len(body)) // 2) + body


def second_record(name, levels, la, lo, x, y, more, codec='latin-1'):
    """One 2ND-STREET record; more is False for the first of a first street's run."""
    assert 0 <= x < 0x800 and 0 <= y < 0x800, (x, y)
    nm = name.encode(codec) if isinstance(name, str) else name
    body = struct.pack('>4H', la, lo, x, y) + _name(nm, levels)
    flags = SECOND_MORE if more else SECOND_FIRST
    return struct.pack('>H', flags << 11 | (2 + len(body)) // 2) + body


def _parse_name(r, o, codec):
    nl, nw = r[o], r[o + 1]
    levels = struct.unpack_from('>%dH' % nl, r, o + 2)
    q = o + 2 + 2 * nl
    return levels, r[q:q + 2 * nw].rstrip(b'\0').decode(codec)


def parse_first(r, codec='latin-1'):
    second_at, count = struct.unpack_from('>IH', r, 2)
    levels, name = _parse_name(r, 8, codec)
    return dict(name=name, levels=levels, second_at=second_at, count=count)


def parse_second(r, codec='latin-1'):
    la, lo, x, y = struct.unpack_from('>4H', r, 2)
    levels, name = _parse_name(r, 10, codec)
    return dict(name=name, levels=levels, la=la, lo=lo, x=x, y=y, more=r[0] >> 3 == SECOND_MORE)


def search_key(name):
    """The folded name the keys are cut from: ADDRE's fold, then a colon or an opening
    bracket is a word break and a closing bracket goes (GREGORIUS IX:N TIE,
    AHVENVAARA (ÖLLÖLÄ), JOKIPELTO(OTALAMPI) in INTER007)."""
    k = fold_key(name).upper().replace(':', ' ').replace('(', ' ').replace(')', '')
    return re.sub(' +', ' ', k).strip()


def word_keys(folded):
    """[(key bytes, is a later word)]: the whole name, and every later word of two or more
    letters to the end of the name. INTER007 keys no one-letter word (A. F. AIRON KATU,
    PAJUUSTONTIE 1); this matches its keys on all but 11 of 132,363 records."""
    out = [(folded.encode('latin-1'), False)]
    for m in re.finditer(r' (\S+)', folded):
        if len(m.group(1)) >= 2:
            out.append((folded[m.start(1):].encode('latin-1'), True))
    return out
