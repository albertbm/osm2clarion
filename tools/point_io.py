# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write POINT<nnn>.DAT, the Clarion place-name search family: the same letter
trie and container as ADDRE, over settlement and facility names rather than streets."""
import struct
import zlib

from addre_io import ALIGN, addr, build_trie, deflate_block, unaddr

REC = 40               # the data record, fixed
REC_W = 20
DF_LEN = 0x200         # POINT's definition region is two blocks, where ADDRE's is one
NAMES_PER_BLOCK = 1000
INFO_PER_BLOCK = 400
SETTLEMENT = 0x1f      # the category code of a place rather than a facility
LANG_LOCAL = 63        # the country's own language, which is where settlements live


def u16(d, o): return struct.unpack_from('>H', d, o)[0]
def u32(d, o): return struct.unpack_from('>I', d, o)[0]


def _split_records(raw):
    """A name or telephone frame is a run of records whose first halfword is flags<<11 | length (ADDRE's convention)."""
    out = []
    q = 0
    while q < len(raw):
        L = (u16(raw, q) & 0x7ff) * 2
        if L == 0:
            break
        out.append(raw[q:q + L])
        q += L
    return out


def _read_frame(d, base, tail_off):
    """(records, blocks, per-block count) of one compressed name frame."""
    per = u16(d, tail_off)
    blocks = []
    o = tail_off + 4
    while True:
        st, cw, uw = struct.unpack_from('>III', d, o)
        if cw == 0:
            break
        blocks.append((st, cw, uw))
        o += 12
    raw = b''.join(zlib.decompress(d[base + st * 2:base + st * 2 + cw * 2])
                   for st, cw, uw in blocks)
    return _split_records(raw), blocks, per


class Point:
    """Parsed file. `keys`, `recs`, `names` and `tels` are the content."""

    def __init__(self, d):
        self.d = d
        self.cr = d[0x1032] * 2
        self.kw = d[0x1033]
        self.nodew = u32(d, 0x1006)
        self.recw = u16(d, 0x101a)
        self.area = u16(d, 0x1040)   # REGION sits where ADDRE has nothing; 6 bytes past ADDRE's 0x103a
        self.rec_off = unaddr(u32(d, 0x1012))
        self.nrec = u32(d, 0x1016) // self.recw
        self.rec_end = self.rec_off + self.nrec * REC
        self.nb = (self.rec_end + DF_LEN + ALIGN - 1) // ALIGN * ALIGN
        self.df = d[self.nb - DF_LEN:self.nb]
        self.nlevel = self.df[0x55]          # the country's ISO tier-label count
        self.prefix = d[:0x1100]
        self.names, self.nblocks, self.per = _read_frame(
            d, self.nb, unaddr(u32(self.df, 0x14c)))
        self.tel_off = unaddr(u32(self.df, 0x106))
        self.tels, self.tblocks, self.tper = _read_frame(
            d, self.tel_off, unaddr(u32(self.df, 0x168)))
        # everything past the two tail tables is further search frames, carried as-is
        t2 = unaddr(u32(self.df, 0x168))
        n = t2 + u32(self.df, 0x16c) * 2 + 4
        self.extra_off = n + (-n) % ALIGN
        self.extra = d[self.extra_off:]
        self._trie()

    def _crec(self, o):
        d = self.d
        return (u32(d, o), u16(d, o + 4), d[o + 8:o + 8 + self.kw],
                int.from_bytes(d[o + 8 + self.kw:o + self.cr], 'big'))

    def _trie(self):
        d = self.d
        o = 0x1100
        root = []
        while u32(d, o):
            root.append(self._crec(o))
            o += self.cr
        self.nroot = len(root)
        keys = []
        stack = [(c, b'') for c in root]
        while stack:
            c, pfx = stack.pop()
            k = pfx + c[2]
            if c[1] == 0:
                keys.append((k, c[0] // self.recw, c[3]))
                continue
            fo = 0x1100 + c[0] * 2
            n = c[1] * 2 // self.cr - 1
            own = int.from_bytes(d[fo + n * self.cr + 8 + self.kw:
                                   fo + (n + 1) * self.cr], 'big')
            if own:
                keys.append((k, u32(d, fo + n * self.cr) // self.recw, own))
            for i in range(n):
                stack.append((self._crec(fo + i * self.cr), k))
        keys.sort()
        self.keys = keys

    def rec(self, i):
        return self.d[self.rec_off + i * REC:self.rec_off + (i + 1) * REC]

    def records(self):
        return [self.rec(i) for i in range(self.nrec)]


def data_record(levels, nlevel, name_idx, category, subcategory=0xffff,
                lat=0, lon=0, ilat=0, ilon=0, lang=LANG_LOCAL,
                flags=0x0000, data_id=0xffffffff):
    """One 40-byte POINT data record; levels is the area ladder, country first, padded to nlevel with 0xffff."""
    assert len(levels) == nlevel, (levels, nlevel)
    lv = tuple(levels) + (0xffff,) * (6 - nlevel)
    return (struct.pack('>6H', *lv)
            + struct.pack('>HHH', flags, category, subcategory)
            + struct.pack('>HHHH', lat, lon, ilat, ilon)
            + struct.pack('>HH', lang << 8, 0)
            + struct.pack('>HI', name_idx, data_id)
            + b'\0\0\0\0')


def name_record(name, levels, nlevel, category, subcategory=0xffff,
                lat=0, lon=0, ilat=0, ilon=0, glat=None, glon=None,
                address=b'', house='', items=b'', refs=((2, 0x40, 0), (3, 0x40, 0)),
                tel_idx=None, flags=0x0c):
    """One POINT name record; the display and address names share one field, separated by '@'."""
    nm = name.encode('utf-8') if isinstance(name, str) else name
    nm += b'\0' * (-len(nm) % 2)
    ad = address.encode('utf-8') if isinstance(address, str) else address
    if ad:
        ad = (house.encode('utf-8') if house else b'') + b'@' + ad
        ad += b'\0' * (-len(ad) % 2)
    ref = b''.join(struct.pack('>BBH', *r) for r in refs)
    t = (0, tel_idx) if tel_idx is not None else (0xffff, 0xffff)
    body = (struct.pack('>HHHH', lat, lon, ilat, ilon)
            + bytes([(4 + len(ref)) // 2, len(items) // 2])
            + struct.pack('>HH', glat if glat is not None else lat,
                          glon if glon is not None else lon)
            + ref + items
            + struct.pack('>HH', *t)
            + bytes([nlevel, 1, 1, 0, len(nm) // 2, len(ad) // 2, 1, 0xff])
            + struct.pack('>%dH' % nlevel, *levels)
            + struct.pack('>HH', category, subcategory)
            + nm + ad
            + struct.pack('>H', subcategory))
    total = 2 + len(body)
    assert total % 2 == 0 and total // 2 < 0x800, (name, total)
    return struct.pack('>H', flags << 11 | total // 2) + body


def tel_record(text, flags=0x00):
    """One telephone-frame record: plain text, counted in halfwords."""
    s = text.encode('ascii') if isinstance(text, str) else text
    s += b'\0' * (-len(s) % 2)
    body = bytes([len(s) // 2, 0xff]) + s
    total = 2 + len(body)
    return struct.pack('>H', flags << 11 | total // 2) + body


def _pack_frame(records, per):
    """(compressed bytes, tail table bytes, block list) for one name frame."""
    blocks = []
    parts = []
    st = 0
    for i in range(0, len(records), per):
        plain = b''.join(records[i:i + per])
        c = deflate_block(plain)
        c += b'\x00' * (-len(c) % 4)
        blocks.append((st, len(c) // 2, len(plain) // 2))
        parts.append(c)
        st += len(c) // 2
    t = bytearray(4 + 12 * len(blocks) + 4)
    struct.pack_into('>HH', t, 0, per, 0)
    o = 4
    for b in blocks:
        struct.pack_into('>III', t, o, *b)
        o += 12
    return b''.join(parts), bytes(t), blocks


def assemble(prefix, df, keys, recs, names, tels, cr, kw, area, nlevel,
             extra=b'', maxkey_hint=None):
    """Bytes of a full POINT file built from prefix, df, keys, recs, names and tels."""
    node, first, _nroot = build_trie(keys, cr, kw, REC_W)
    out = bytearray(prefix)
    out[0x1032] = cr // 2
    out[0x1033] = kw
    struct.pack_into('>I', out, 0x1006, len(node) // 2)
    struct.pack_into('>H', out, 0x100e, first)
    struct.pack_into('>I', out, 0x1016, len(recs) * REC_W)
    struct.pack_into('>H', out, 0x101a, REC_W)
    struct.pack_into('>H', out, 0x1040, area)
    out += node
    rb = len(out) + (-len(out)) % ALIGN
    struct.pack_into('>I', out, 0x1012, addr(rb))
    out += b'\x00' * (rb - len(out))
    for r in recs:
        assert len(r) == REC, len(r)
        out += r
    nb = (len(out) + DF_LEN + ALIGN - 1) // ALIGN * ALIGN
    out += b'\x00' * (nb - DF_LEN - len(out))
    d = bytearray(df)
    assert len(d) == DF_LEN

    ncmp, ntail, nblocks = _pack_frame(names, NAMES_PER_BLOCK)
    tcmp, ttail, tblocks = _pack_frame(tels, INFO_PER_BLOCK)
    def up(x):
        return x + (-x) % ALIGN
    # every frame starts on, and is padded out to, a 256-byte block
    tel_off = up(nb + len(ncmp))
    P = up(tel_off + max(len(tcmp), 1))
    P2 = up(P + len(ntail))
    end = up(P2 + len(ttail))

    struct.pack_into('>I', d, 0x02e, addr(nb))                       # name frame
    struct.pack_into('>I', d, 0x032, sum(b[1] for b in nblocks))
    struct.pack_into('>H', d, 0x03a, max(len(n) for n in names) // 2)
    if maxkey_hint is not None:
        struct.pack_into('>H', d, 0x03c, maxkey_hint)
    d[0x055] = nlevel
    struct.pack_into('>H', d, 0x0b8, _maxfield(names, 0))            # longest name
    struct.pack_into('>H', d, 0x0d6, _maxfield(names, 1))            # longest address
    struct.pack_into('>I', d, 0x106, addr(tel_off))                  # telephone frame
    struct.pack_into('>I', d, 0x10a, sum(b[1] for b in tblocks))
    struct.pack_into('>I', d, 0x10e, 0)
    struct.pack_into('>I', d, 0x14c, addr(P))
    struct.pack_into('>I', d, 0x150, (4 + 12 * len(nblocks)) // 2)
    struct.pack_into('>H', d, 0x154, NAMES_PER_BLOCK)
    struct.pack_into('>I', d, 0x168, addr(P2))
    struct.pack_into('>I', d, 0x16c, (4 + 12 * len(tblocks)) // 2)
    struct.pack_into('>H', d, 0x170, INFO_PER_BLOCK)
    out += d
    assert len(out) == nb

    struct.pack_into('>I', out, 0x824 + 6, addr(nb - DF_LEN))        # slot 7
    struct.pack_into('>H', out, 0x824 + 10, DF_LEN // ALIGN)
    if not extra:
        # with no extra search frames, slots 8..299 clear, leaving 6 and 7 live (ADDRE's own shape)
        for i in range(8, 300):
            struct.pack_into('>IH', out, 0x800 + i * 6, 0xffffffff, 0)

    out += ncmp
    out += b'\x00' * (tel_off - len(out))
    out += tcmp
    out += b'\x00' * (P - len(out))
    out += ntail
    out += b'\x00' * (P2 - len(out))
    out += ttail
    out += b'\x00' * (end - len(out))
    out += extra
    return bytes(out), nblocks, tblocks


def _maxfield(names, i):
    """The longest name (i=0) or address (i=1) in halfwords, over every record."""
    best = 0
    for n in names:
        p = 16 + (n[10] * 2 - 4) + n[11] * 2 + 8
        best = max(best, n[p + i])
    return best


# Slot 278 at 0x800: one 72-byte entry per category, laid out like the main header with
# CATEGORY in place of REGION. Slot 279 holds (category, sub-category) pairs, 78-byte entries.
# Slot 15: a 28-byte entry per category pointing at the nearby-search block nearby_frame writes.
CAT_ENTRY = 72
CAT_CR, CAT_KW = 14, 2


def category_entry(ordinal, area, code, trie_at, nodew, first, rec_at, nrec, last=False):
    e = bytearray(CAT_ENTRY)
    # +0 is the next entry's offset in halfwords; the last entry writes 0xffff there and at +0x30
    struct.pack_into('>H', e, 0, 0xffff if last else CAT_ENTRY // 2 * (ordinal + 1))
    struct.pack_into('>I', e, 2, addr(trie_at))
    struct.pack_into('>I', e, 6, nodew)
    struct.pack_into('>H', e, 0xe, first)
    struct.pack_into('>H', e, 0x10, 0x4c02)
    struct.pack_into('>I', e, 0x12, addr(rec_at))
    struct.pack_into('>I', e, 0x16, nrec * REC_W)
    struct.pack_into('>H', e, 0x1a, REC_W)
    struct.pack_into('>H', e, 0x30, 0xffff if last else CAT_ENTRY // 2)
    e[0x32], e[0x33] = CAT_CR // 2, CAT_KW
    struct.pack_into('>HHH', e, 0x34, ordinal, 0x0800, 3)
    e[0x3a:0x42] = b'CATEGORY'
    struct.pack_into('>HHH', e, 0x42, area, 0xffff, code)
    return bytes(e)


def nearby_frame(la, lo, items):
    """The slot-15 block for a category whose records all sit in one level-1 parcel (la, lo):
    three narrowing levels, then a leaf of 16-byte items, one per name."""
    def level(nxt, la_, lo_, tail, words=0xd):
        # word 3 is the halfword count of what the level points at: 8 per entry plus 5
        b = bytearray(ALIGN)
        struct.pack_into('>8H', b, 0, 8, 0, nxt, words, la_, lo_, 0, 0)
        b[16:16 + len(tail)] = tail
        return bytes(b)
    leaf = bytearray()
    for name_idx, cat, ilat, ilon, lang in sorted(items, key=lambda t: (t[3] & 0x7ff, t[2] & 0x7ff, t[0])):
        # the category goes in as BCD: 24 reads 0x0024, 71 0x0071
        leaf += struct.pack('>8H', 8, 0, name_idx, 0, 0, int(str(cat), 16),
                            (lang >> 5) << 15 | ilat & 0x7fff, (lang & 0x1f) << 11 | ilon & 0x7ff)
    leaf += bytes.fromhex('0005ffffffff00000100000000000000')
    leaf += b'\0' * (-len(leaf) % ALIGN)
    return (level(0x80, la & 0xff00, lo & 0xff00, bytes.fromhex('0005ffffffffffffffff'))
            + level(0x100, la & 0xfff0, lo & 0xfff0, bytes.fromhex('0005ffffffff0000'))
            + level(0x180, la, lo, bytes.fromhex('0005ffffffff000000800000'), 8 * len(items) + 5)
            + bytes(leaf))


def category_frames(at, area, cats):
    """Bytes and slot 15 / slot 278 table values for a set of categories, each in one parcel."""
    assert at % ALIGN == 0
    n = len(cats)
    t15 = bytearray(ALIGN * ((28 * n + ALIGN - 1) // ALIGN))
    t278 = bytearray(ALIGN * ((CAT_ENTRY * n + ALIGN - 1) // ALIGN))
    # file order: slot-15 table, its blocks, slot-278 table, then each category's trie and records
    near = bytearray()
    p = at + len(t15)
    for i, (code, keys, recs, (la, lo)) in enumerate(cats):
        items, seen = [], set()
        for r in recs:
            w = struct.unpack('>20H', r)
            if w[15] not in seen:
                seen.add(w[15]); items.append((w[15], code, w[11], w[12], w[13] >> 8))
        nb = nearby_frame(la, lo, items)
        struct.pack_into('>14H', t15, 28 * i,
                         (28 * (i + 1)) // 2 if i + 1 < n else 0xffff, 0, addr(p + len(near)), 0,
                         len(nb) // 2, 0, 0, 0xd, 0x8000, 8, i, 0, 1, code)
        near += nb
    at278 = p + len(near)
    # every category's trie, then every category's records, as the factory lays them out
    body = bytearray()
    p = at278 + len(t278)
    tries = []
    for code, keys, recs, _ in cats:
        node, first, _n = build_trie(keys, CAT_CR, CAT_KW, REC_W)
        tries.append((p + len(body), len(node) // 2, first))
        body += node + b'\0' * (-len(node) % ALIGN)
    for i, (code, keys, recs, _) in enumerate(cats):
        rec_at = p + len(body)
        for r in recs:
            assert len(r) == REC
            body += r
        body += b'\0' * (-len(body) % ALIGN)
        trie_at, nodew, first = tries[i]
        t278[CAT_ENTRY * i:CAT_ENTRY * (i + 1)] = category_entry(
            i, area, code, trie_at, nodew, first, rec_at, len(recs), last=i + 1 == n)
    return (bytes(t15) + bytes(near) + bytes(t278) + bytes(body),
            (addr(at), len(t15) // ALIGN), (addr(at278), len(t278) // ALIGN))
