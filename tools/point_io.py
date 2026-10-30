# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write POINT<nnn>.DAT, the Clarion place-name search family: the same letter
trie and container as ADDRE, over settlement and facility names rather than streets."""
import struct
import unicodedata
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
                lat=0, lon=0, x=0, y=0, lang=LANG_LOCAL,
                flags=0x0000, data_id=0xffffffff):
    """One 40-byte POINT data record; levels is the area ladder, country first, padded to
    nlevel with 0xffff; lat, lon the level-1 parcel code and x, y the position in it, in
    2048ths of the parcel, written x first as 0x800 | x (POINT018: CARLTON 09c9 06ed)."""
    assert len(levels) == nlevel, (levels, nlevel)
    assert 0 <= x < 0x800 and 0 <= y < 0x800, (x, y)
    lv = tuple(levels) + (0xffff,) * (6 - nlevel)
    return (struct.pack('>6H', *lv)
            + struct.pack('>HHH', flags, category, subcategory)
            + struct.pack('>HHHH', lat, lon, 0x800 | x, y)
            + struct.pack('>HH', lang << 8, 0)
            + struct.pack('>HI', name_idx, data_id)
            + b'\0\0\0\0')


def parse_data_record(r):
    """The fields of one data record, named as data_record takes them."""
    w = struct.unpack('>20H', r)
    return dict(levels=w[:6], flags=w[6], category=w[7], subcategory=w[8], lat=w[9], lon=w[10],
                x=w[11] & 0x7ff, y=w[12], lang=w[13] >> 8, name_idx=w[15], data_id=w[16] << 16 | w[17])


def name_record(name, levels, nlevel, category, subcategory=0xffff,
                lat=0, lon=0, x=0, y=0, glat=None, glon=None,
                address=b'', house='', items=b'', refs=((2, 0x40, 0), (3, 0x40, 0)),
                tel_idx=None, flags=0x0c):
    """One POINT name record; the display and address names share one field, separated by '@'.
    lat, lon, x, y as in data_record."""
    assert 0 <= x < 0x800 and 0 <= y < 0x800, (x, y)
    nm = name.encode('utf-8') if isinstance(name, str) else name
    nm += b'\0' * (-len(nm) % 2)
    ad = address.encode('utf-8') if isinstance(address, str) else address
    if ad:
        ad = (house.encode('utf-8') if house else b'') + b'@' + ad
        ad += b'\0' * (-len(ad) % 2)
    ref = b''.join(struct.pack('>BBH', *r) for r in refs)
    t = (0, tel_idx) if tel_idx is not None else (0xffff, 0xffff)
    body = (struct.pack('>HHHH', lat, lon, 0x800 | x, y)
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


def parse_name_record(nr):
    """The fields of one name record, named as name_record takes them, so that
    name_record(**parse_name_record(nr)) == nr."""
    lat, lon, xw, y = struct.unpack_from('>HHHH', nr, 2)
    nref = (nr[10] * 2 - 4) // 4
    glat, glon = struct.unpack_from('>HH', nr, 12)
    refs = tuple(struct.unpack_from('>BBH', nr, 16 + 4 * i) for i in range(nref))
    o = 16 + 4 * nref
    items = nr[o:o + nr[11] * 2]
    o += nr[11] * 2
    t0, t1 = struct.unpack_from('>HH', nr, o)
    o += 4
    nlevel, nmlen, adlen = nr[o], nr[o + 4], nr[o + 5]
    o += 8
    levels = struct.unpack_from('>%dH' % nlevel, nr, o)
    o += 2 * nlevel
    category, subcategory = struct.unpack_from('>HH', nr, o)
    o += 4
    name = nr[o:o + 2 * nmlen].rstrip(b'\0').decode('utf-8')
    o += 2 * nmlen
    ad = nr[o:o + 2 * adlen].rstrip(b'\0').decode('utf-8')
    house, address = ad.split('@', 1) if '@' in ad else ('', ad)
    return dict(name=name, levels=levels, nlevel=nlevel, category=category, subcategory=subcategory,
                lat=lat, lon=lon, x=xw & 0x7ff, y=y, glat=glat, glon=glon, address=address, house=house,
                items=items, refs=refs, tel_idx=None if t0 == 0xffff else t1, flags=nr[0] >> 3)


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


L1_MASK, L2_MASK = 0xff80, 0xfff0   # how the first two tree levels coarsen a parcel code


def _bcd(n):
    return int(str(n), 16)


def collate(name):
    """The order a nearby leaf sorts names in: letter by letter, an accented letter right
    after its base letter (NÁRODNÉ before NEMZETI, LÜKSEMBURG after LUXENBORG)."""
    return tuple((c[0], c[1:]) for c in (unicodedata.normalize('NFD', ch) for ch in name))


def nearby_frame(items, sub=False):
    """The slot-15 block of one category: a tree over the records' level-1 parcel codes,
    three narrowing levels keyed (la & 0xff80, lo & 0xff80), (la & 0xfff0, lo & 0xfff0) and
    (la, lo), then a leaf of 16-byte items per parcel. A category with a sub-category list
    (sub=True: hotels, restaurants, petrol) has one more level under each parcel, a single
    entry keyed (0, category), and its items carry the sub-category where the others carry
    the category. Blocks are laid out depth first, each 256-aligned; an entry points at its
    child block in halfwords from the frame start (u32: restaurants pass 64K words) and
    carries that block's content size, 8 words per entry plus the 5-word terminator, whose
    u32 tail points back at the parent. A leaf is sorted by code, y, x, then the name
    (collate) and language. items: [(name_idx, cat, sub, la, lo, xw, y, lang, name)] with
    xw the 0x800 | x word. Reproduces all 56 category blocks of POINT018 byte for byte."""
    tree = {}
    for it in items:
        la, lo = it[3], it[4]
        assert len(it) == 9, it
        tree.setdefault((la & L1_MASK, lo & L1_MASK), {}).setdefault(
            (la & L2_MASK, lo & L2_MASK), {}).setdefault((la, lo), []).append(it)
    blocks = []     # [entries, parent block index or None]

    def add(keys, parent):
        blocks.append([[[la, lo] for la, lo in keys], parent])
        return len(blocks) - 1

    def leaf(group, parent):
        # the code goes in as BCD: category 24 reads 0x0024, cuisine 907 0x0907
        rows = sorted((_bcd(sb if sb != 0xffff else cat), y & 0x7ff, xw & 0x7ff, collate(name), lang, name_idx, xw)
                      for name_idx, cat, sb, la, lo, xw, y, lang, name in group)
        blocks.append([[struct.pack('>HIHHHHH', 8, n, 0, 0, code, (lang >> 5) << 15 | xw & 0x7fff, (lang & 0x1f) << 11 | y)
                        for code, y, x, name, lang, n, xw in rows], parent])
        return len(blocks) - 1

    l1 = add(sorted(tree), None)
    for e1, k1 in zip(blocks[l1][0], sorted(tree)):
        l2 = add(sorted(tree[k1]), l1)
        e1.append(l2)
        for e2, k2 in zip(blocks[l2][0], sorted(tree[k1])):
            l3 = add(sorted(tree[k1][k2]), l2)
            e2.append(l3)
            for e3, k3 in zip(blocks[l3][0], sorted(tree[k1][k2])):
                group = tree[k1][k2][k3]
                if sub:
                    l4 = add([(0, group[0][1])], l3)
                    blocks[l4][0][0].append(leaf(group, l4))
                    e3.append(l4)
                else:
                    e3.append(leaf(group, l3))
    at, pos = [], 0
    for entries, parent in blocks:
        at.append(pos)
        n = 16 * len(entries) + 10
        pos += n + (-n) % ALIGN
    out = bytearray()
    for entries, parent in blocks:
        for e in entries:
            if isinstance(e, list):      # a key entry: child pointer and the child's content size
                la, lo, child = e
                out += struct.pack('>HIHHHHH', 8, at[child] // 2, 8 * len(blocks[child][0]) + 5, la, lo, 0, 0)
            else:
                out += e
        out += struct.pack('>HHHI', 5, 0xffff, 0xffff, 0xffffffff if parent is None else at[parent] // 2)
        out += b'\0' * (-len(out) % ALIGN)
    return bytes(out)


def parse_nearby(d, base, nl1, names=()):
    """(items, sub) back out of a nearby_frame block at file offset base whose level-1 block
    has nl1 entries (the slot-15 table's word 7 is 8 * nl1 + 5); names, the file's name
    records, fill in the item names."""
    sub = [False]

    def block(off, n):
        return [struct.unpack_from('>HIHHHHH', d, off + 16 * i) for i in range(n)]

    def walk(off, n, depth):
        out = []
        for w in block(off, n):
            assert w[0] == 8, (hex(off), w)
            child, cn = base + 2 * w[1], (w[2] - 5) // 8
            if depth < 3:
                out += walk(child, cn, depth + 1)
                continue
            cat = None
            if block(child, 1)[0][2]:        # a size word: the (0, category) level under a parcel, not a leaf
                sub[0] = True
                (e,) = block(child, cn)
                child, cn, cat = base + 2 * e[1], (e[2] - 5) // 8, e[4]
            for it in block(child, cn):
                code = int('%04x' % it[4])
                out.append((it[1], cat if cat is not None else code, code if cat is not None else 0xffff, w[3], w[4],
                            it[5] & 0x7fff, it[6] & 0x7ff, (it[5] >> 15) << 5 | it[6] >> 11,
                            parse_name_record(names[it[1]])['name'] if names else ''))
        return out
    items = walk(base, nl1, 1)
    return items, sub[0]


def category_frames(at, area, cats, names):
    """Bytes and slot 15 / slot 278 table values for a set of categories [(code, keys, recs)];
    names are the file's name records, which the nearby leaves are sorted by."""
    assert at % ALIGN == 0
    n = len(cats)
    t15 = bytearray(ALIGN * ((28 * n + ALIGN - 1) // ALIGN))
    t278 = bytearray(ALIGN * ((CAT_ENTRY * n + ALIGN - 1) // ALIGN))
    # file order: slot-15 table, its blocks, slot-278 table, then each category's trie and records
    near = bytearray()
    p = at + len(t15)
    for i, (code, keys, recs) in enumerate(cats):
        items, seen = [], set()
        for r in recs:
            w = struct.unpack('>20H', r)
            if w[15] not in seen:
                seen.add(w[15])
                items.append((w[15], code, w[8], w[9], w[10], w[11], w[12], w[13] >> 8,
                              parse_name_record(names[w[15]])['name']))
        nb = nearby_frame(items, sub=any(it[2] != 0xffff for it in items))
        nl1 = len({(it[3] & L1_MASK, it[4] & L1_MASK) for it in items})
        struct.pack_into('>HIIHHHHHHHHH', t15, 28 * i,
                         (28 * (i + 1)) // 2 if i + 1 < n else 0xffff, addr(p + len(near)),
                         len(nb) // 2, 0, 0, 8 * nl1 + 5, 0x8000, 8, i, 0, 1, code)
        near += nb
    at278 = p + len(near)
    # every category's trie, then every category's records, as the factory lays them out
    body = bytearray()
    p = at278 + len(t278)
    tries = []
    for code, keys, recs in cats:
        node, first, _n = build_trie(keys, CAT_CR, CAT_KW, REC_W)
        tries.append((p + len(body), len(node) // 2, first))
        body += node + b'\0' * (-len(node) % ALIGN)
    for i, (code, keys, recs) in enumerate(cats):
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
