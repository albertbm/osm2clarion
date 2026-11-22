# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write POINT<nnn>.DAT in the X08-02 (QY7) layout.

Measured on POINT007 (Finland) of the 2014 QY7 card. The search frames are the X08-03 ones
(point_io), packed the way an X08-02 ADDRE is, in this file order:

* the main trie (cr 12, kw 1) and its 24-byte rows in zlib streams: ladder (5 halfwords),
  ffff, flags, category, sub-category, language << 8, and the FACILITY record index (u32).
  A row has no position, unlike X08-03. The flags word is 0 on every row of six factory
  files (Finland, France, Greece, Poland, Hungary, Spain: 6.2 million main, category and
  sub-category rows), later-word keys included, where X08-03 sets 0x4000.
* slot 15, the nearby search: point_io's 28-byte table and parcel tree, with a leaf sorted by
  x, y, code, then the Latin-1 name (X08-03: code, y, x, collated name, language).
* slot 278, CATEGORY: a 70-byte entry per category (no ffff word in the tail), then every
  category's trie, then every category's rows, packed as the main rows are.
* slot 279, SUB CATEGORY: the same with 76-byte entries.
* slot 7, the 512-byte definition region; FACILITY (1000 records a block) and INFORMATION
  (400) name frames and their two tail tables.
* slots 280 and 287, the ADM frame (area names) and its definition region, to the end.

A FACILITY record is point_io's name record with two more bytes in its name header: a zero
after the 1, 1, 0 run and a zero ahead of the 1, ff pair.
"""
import struct
import zlib

import inter_io
from addre_io import ALIGN, V5_PER, addr, build_trie, unaddr
from point_io import _bcd, _read_frame

CR, KW = 12, 1
ROW = 24
ROW_W = 12
ADM_ROW, ADM_ROW_W = 18, 9
DF_LEN = 0x200
FAC_PER, INF_PER, ADM_PER = 1000, 400, 2000
CAT_ENTRY, SUB_ENTRY = 70, 76
NEAR_ENTRY = 28
SETTLEMENT = 0x1f
LANG_LOCAL = 63
LATER = 0x4000

# definition region: (frame address, compressed words) and (tail address, tail words, per block)
DF_FAC, DF_INF = (0x2e, 0x32), (0x13c, 0x140)
DF_FAC_TAIL, DF_INF_TAIL = 0x19a, 0x1b6
# ADM definition region
ADM_DF_FRAME, ADM_DF_TAIL = (0x24, 0x28), 0x94


def u16(d, o): return struct.unpack_from('>H', d, o)[0]
def u32(d, o): return struct.unpack_from('>I', d, o)[0]


def up(n):
    return n + (-n) % ALIGN


def trie_keys(d, base, recw=ROW_W, cr=CR, kw=KW):
    """[(key, first row, count)] of a trie whose root ends at its all-ones key."""
    def crec(o):
        return u32(d, o), u16(d, o + 4), d[o + 8:o + 8 + kw], int.from_bytes(d[o + 8 + kw:o + cr], 'big')
    o, root = base, []
    while d[o + 8:o + 8 + kw] != b'\xff' * kw:
        root.append(crec(o))
        o += cr
    keys, stack = [], [(c, b'') for c in root]
    while stack:
        c, pfx = stack.pop()
        k = pfx + c[2]
        if c[1] == 0:
            keys.append((k, c[0] // recw, c[3]))
            continue
        fo = base + c[0] * 2
        n = c[1] * 2 // cr - 1
        own = int.from_bytes(d[fo + n * cr + 8 + kw:fo + (n + 1) * cr], 'big')
        if own:
            keys.append((k, u32(d, fo + n * cr) // recw, own))
        for i in range(n):
            stack.append((crec(fo + i * cr), k))
    return sorted(keys)


def read_rows(d, off, n, size=ROW):
    """n rows of size bytes from a packed row block at off."""
    assert u16(d, off) == V5_PER, hex(u16(d, off))
    m = -(-n // V5_PER)
    p = off + 4 + 4 * m
    out = b''
    for i in range(m):
        z = u32(d, off + 4 + 4 * i) * 2
        out += zlib.decompress(d[p:p + z])
        p += z
    assert len(out) == n * size, (len(out), n)
    return [out[i:i + size] for i in range(0, len(out), size)]


def pack_rows(rows, deflate):
    plain = b''.join(rows)
    step = V5_PER * len(rows[0])
    parts = [deflate(plain[i:i + step]) for i in range(0, len(plain), step)]
    return struct.pack('>HH', V5_PER, 0) + b''.join(struct.pack('>I', len(c) // 2) for c in parts) + b''.join(parts)


def pack_frame(recs, per, deflate):
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


def tail_table(blocks, per):
    t = bytearray(4 + 12 * len(blocks) + 4)
    struct.pack_into('>HH', t, 0, per, 0)
    for i, b in enumerate(blocks):
        struct.pack_into('>III', t, 4 + 12 * i, *b)
    return bytes(t)


def row(levels, name_idx, category, subcategory=0xffff, lang=LANG_LOCAL, later=False):
    lv = tuple(levels) + (0xffff,) * (5 - len(levels))
    return struct.pack('>5HHHHHHI', *lv, 0xffff, LATER if later else 0, category, subcategory, lang << 8, name_idx)


def parse_row(r):
    w = struct.unpack('>5HHHHHHI', r)
    return dict(levels=w[:5], later=w[6] == LATER, category=w[7], subcategory=w[8], lang=w[9] >> 8, name_idx=w[10])


def name_record(name, levels, category, subcategory=0xffff, lat=0, lon=0, xw=0x800, y=0, glat=None, glon=None,
                refs=((2, 0x40, 0), (3, 0x40, 0)), items=b'', address=b'', tel=(0xffff, 0xffff), flags=0x0c,
                tail=None):
    """One FACILITY record. name and address are bytes as stored (Latin-1, the address
    house@street); xw is the x word as stored, 0x800 | x. tail defaults to the sub-category."""
    nm = name + b'\0' * (len(name) % 2)
    ad = address + b'\0' * (len(address) % 2)
    ref = b''.join(struct.pack('>BBH', *r) for r in refs)
    body = (struct.pack('>HHHH', lat, lon, xw, y)
            + bytes([(4 + len(ref)) // 2, len(items) // 2])
            + struct.pack('>HH', lat if glat is None else glat, lon if glon is None else glon)
            + ref + items + struct.pack('>HH', *tel)
            + bytes([len(levels), 1, 1, 0, 0, len(nm) // 2, len(ad) // 2, 0, 1, 0xff])
            + struct.pack('>%dH' % len(levels), *levels)
            + struct.pack('>HH', category, subcategory) + nm + ad
            + (struct.pack('>H', subcategory) if tail is None else tail))
    total = 2 + len(body)
    assert total % 2 == 0 and total // 2 < 0x800, (name, total)
    return struct.pack('>H', flags << 11 | total // 2) + body


def parse_name_record(r):
    """name_record's arguments back, so name_record(**parse_name_record(r)) == r."""
    lat, lon, xw, y = struct.unpack_from('>4H', r, 2)
    nref = (r[10] * 2 - 4) // 4
    glat, glon = struct.unpack_from('>HH', r, 12)
    refs = tuple(struct.unpack_from('>BBH', r, 16 + 4 * i) for i in range(nref))
    o = 16 + 4 * nref
    items = r[o:o + r[11] * 2]
    o += r[11] * 2
    tel = struct.unpack_from('>HH', r, o)
    o += 4
    nl, nmw, adw = r[o], r[o + 5], r[o + 6]
    assert r[o + 1:o + 5] == b'\1\1\0\0' and r[o + 7:o + 10] == b'\0\1\xff', r[o:o + 10].hex()
    o += 10
    levels = struct.unpack_from('>%dH' % nl, r, o)
    o += 2 * nl
    category, subcategory = struct.unpack_from('>HH', r, o)
    o += 4
    name = r[o:o + 2 * nmw]
    o += 2 * nmw
    address = r[o:o + 2 * adw]
    o += 2 * adw
    tail = r[o:]
    out = dict(name=name, levels=levels, category=category, subcategory=subcategory, lat=lat, lon=lon, xw=xw, y=y,
               glat=glat, glon=glon, refs=refs, items=items, address=address, tel=tel, flags=r[0] >> 3, tail=tail)
    # an odd-length text keeps its pad byte as stored
    for k in ('name', 'address'):
        if out[k][-1:] == b'\0' and len(out[k].rstrip(b'\0')) % 2:
            out[k] = out[k][:-1]
    return out


def search_key(name):
    """The folded name POI keys are cut from: INTER's fold with commas and full stops as word
    breaks (AJANVAUNU OY, MIKKELI keys OY MIKKELI; a QY8 card's APOTEKA 36,6 keys 36 6). All
    but 116 of POINT007's 68,847 records have a whole-name key that this fold starts with."""
    return inter_io.search_key(name.replace(',', ' ').replace('.', ' '))


def text(name):
    """A stored name without its padding."""
    return name.rstrip(b'\0')


L1_MASK, L2_MASK = 0xff80, 0xfff0   # how the first two tree levels coarsen a parcel code


def nearby_frame(items, sub=False):
    """The slot-15 block of one category. items: [(name_idx, cat, sub, la, lo, xw, y, lang,
    name bytes)], xw the x word with any flag bits it carries. A leaf is sorted by x, y,
    code and name; the sort is stable, so items tied on all four keep their given order
    (11 of POINT007's 31,225 leaves hold such ties, the same name in two languages, in an
    order no field explains)."""
    tree = {}
    for it in items:
        la, lo = it[3], it[4]
        tree.setdefault((la & L1_MASK, lo & L1_MASK), {}).setdefault(
            (la & L2_MASK, lo & L2_MASK), {}).setdefault((la, lo), []).append(it)
    blocks = []

    def add(keys, parent):
        blocks.append([[[la, lo] for la, lo in keys], parent])
        return len(blocks) - 1

    def leaf(group, parent):
        rows = [(xw & 0x7ff, y & 0x7ff, _bcd(sb if sb != 0xffff else cat), name, lang, name_idx, xw)
                for name_idx, cat, sb, la, lo, xw, y, lang, name in group]
        rows.sort(key=lambda r: r[:4])
        blocks.append([[struct.pack('>HIHHHHH', 8, n, 0, 0, code, (lang >> 5) << 15 | xw & 0x7fff,
                                    (lang & 0x1f) << 11 | y)
                        for x, y, code, name, lang, n, xw in rows], parent])
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
            if isinstance(e, list):
                la, lo, child = e
                out += struct.pack('>HIHHHHH', 8, at[child] // 2, 8 * len(blocks[child][0]) + 5, la, lo, 0, 0)
            else:
                out += e
        out += struct.pack('>HHHI', 5, 0xffff, 0xffff, 0xffffffff if parent is None else at[parent] // 2)
        out += b'\0' * (-len(out) % ALIGN)
    return bytes(out)


def parse_nearby(d, base, nl1):
    """(items without names, sub) of a nearby block, leaves in stored order; items as
    nearby_frame takes them, the name left None."""
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
            if block(child, 1)[0][2]:
                sub[0] = True
                (e,) = block(child, cn)
                child, cn, cat = base + 2 * e[1], (e[2] - 5) // 8, e[4]
            for it in block(child, cn):
                code = int('%04x' % it[4])
                out.append((it[1], cat if cat is not None else code, code if cat is not None else 0xffff, w[3], w[4],
                            it[5] & 0x7fff, it[6] & 0x7ff, (it[5] >> 15) << 5 | it[6] >> 11, None))
        return out
    return walk(base, nl1, 1), sub[0]


def _entry(size, label, kind, ordinal, tailw, trie_at, nodew, first, rec_at, nrec, last):
    """One CATEGORY (kind 2) or SUB CATEGORY (kind 3) table entry."""
    e = bytearray(size)
    struct.pack_into('>H', e, 0, 0xffff if last else size // 2 * (ordinal + 1))
    struct.pack_into('>I', e, 2, addr(trie_at))
    struct.pack_into('>I', e, 6, nodew)
    struct.pack_into('>H', e, 0xe, first)
    struct.pack_into('>H', e, 0x10, 0x4c40)
    struct.pack_into('>I', e, 0x12, addr(rec_at))
    struct.pack_into('>I', e, 0x16, nrec * ROW_W)
    e[0x32], e[0x33] = CR // 2, KW
    struct.pack_into('>HHH', e, 0x34, ordinal, len(label) << 8, kind)
    e[0x3a:0x3a + len(label)] = label
    struct.pack_into('>%dH' % len(tailw), e, size - 2 * len(tailw), *tailw)
    return bytes(e)


def parse_table(d, slot, size):
    """[(tail words, keys, rows)] of the CATEGORY (278) or SUB CATEGORY (279) table."""
    a, _nb = struct.unpack_from('>IH', d, 0x800 + 6 * slot)
    if a == 0xffffffff:
        return []
    o, out = unaddr(a), []
    ntail = 2 if size == CAT_ENTRY else 3
    while True:
        e = d[o:o + size]
        keys = trie_keys(d, unaddr(u32(e, 2)))
        rows = read_rows(d, unaddr(u32(e, 0x12)), u32(e, 0x16) // ROW_W)
        out.append((struct.unpack_from('>%dH' % ntail, e, size - 2 * ntail), keys, rows))
        if u16(e, 0) == 0xffff:
            return out
        o += size


def table_frames(at, groups, size, deflate):
    """(bytes, (address, blocks)) of a CATEGORY or SUB CATEGORY table and its tries and rows;
    groups [(tail words, keys, rows)]."""
    label, kind = (b'CATEGORY', 2) if size == CAT_ENTRY else (b'SUB CATEGORY', 3)
    n = len(groups)
    table = bytearray(up(size * n))
    body = bytearray()
    p = at + len(table)
    tries = []
    for _t, keys, _rows in groups:
        node, first, _ = build_trie(keys, CR, KW, ROW_W)
        tries.append((p + len(body), len(node) // 2, first))
        body += node + b'\0' * (-len(node) % ALIGN)
    for i, (tailw, _keys, rows) in enumerate(groups):
        rec_at = p + len(body)
        body += pack_rows(rows, deflate)
        body += b'\0' * (-len(body) % ALIGN)
        table[size * i:size * (i + 1)] = _entry(size, label, kind, i, tailw, *tries[i], rec_at, len(rows), i + 1 == n)
    return bytes(table) + bytes(body), (addr(at), len(table) // ALIGN)


class Point:
    """Parsed file: every frame as content, enough for assemble to give the file back."""

    def __init__(self, d):
        self.d = d
        assert d[0x1032] * 2 == CR and d[0x1033] == KW
        self.prefix = d[:0x1100]
        self.area = u16(d, 0x1040)
        self.keys = trie_keys(d, 0x1100)
        self.rows = read_rows(d, unaddr(u32(d, 0x1012)), u32(d, 0x1016) // ROW_W)
        a15, n15 = struct.unpack_from('>IH', d, 0x800 + 6 * 15)
        self.near = []          # [(table words, items, sub)]
        o = unaddr(a15)
        while True:
            w = struct.unpack_from('>HIIHHHHHHHHH', d, o)
            items, sub = parse_nearby(d, unaddr(w[1]), (w[5] - 5) // 8)
            self.near.append((w, items, sub))
            if w[0] == 0xffff:
                break
            o += NEAR_ENTRY
        self.cats = parse_table(d, 278, CAT_ENTRY)
        self.subs = parse_table(d, 279, SUB_ENTRY)
        dfr = unaddr(u32(d, 0x800 + 6 * 7))
        self.df = d[dfr:dfr + DF_LEN]
        self.names, _, _ = _read_frame(d, unaddr(u32(self.df, DF_FAC[0])), unaddr(u32(self.df, DF_FAC_TAIL)))
        self.infos, _, _ = _read_frame(d, unaddr(u32(self.df, DF_INF[0])), unaddr(u32(self.df, DF_INF_TAIL)))
        h = unaddr(u32(d, 0x800 + 6 * 280))
        self.adm_head = d[h:h + 0x100]
        self.adm_keys = trie_keys(d, unaddr(u32(d, h + 2)), ADM_ROW_W)
        self.adm_rows = read_rows(d, unaddr(u32(d, h + 0x12)), u32(d, h + 0x16) // ADM_ROW_W, ADM_ROW)
        a = unaddr(u32(d, 0x800 + 6 * 287))
        self.adm_df = d[a:a + 0x100]
        self.adm_names, _, _ = _read_frame(d, unaddr(u32(self.adm_df, ADM_DF_FRAME[0])),
                                           unaddr(u32(self.adm_df, ADM_DF_TAIL)))


def near_frames(at, near):
    """(bytes, (address, blocks)) of the slot-15 table and its trees; near [(code, items, sub)]."""
    n = len(near)
    table = bytearray(up(NEAR_ENTRY * n))
    body = bytearray()
    p = at + len(table)
    for i, (code, items, sub) in enumerate(near):
        nb = nearby_frame(items, sub)
        nl1 = len({(it[3] & L1_MASK, it[4] & L1_MASK) for it in items})
        # the size leaves out the last block's padding
        last = len(nb) - len(nb.rstrip(b'\0'))
        size = len(nb) - (last - last % 2)
        struct.pack_into('>HIIHHHHHHHHH', table, NEAR_ENTRY * i, NEAR_ENTRY // 2 * (i + 1) if i + 1 < n else 0xffff,
                         addr(p + len(body)), size // 2, 0, 0, 8 * nl1 + 5, 0x8000, 8, i, 0, 1, code)
        body += nb
    return bytes(table) + bytes(body), (addr(at), len(table) // ALIGN)


def adm_frame(at, head, df, keys, rows, names, deflate):
    """(bytes, header offset, definition-region offset) of the ADM frame at file offset at."""
    node, first, _ = build_trie(keys, CR, KW, ADM_ROW_W)
    h = bytearray(head)
    struct.pack_into('>I', h, 0x02, addr(at + 0x100))
    struct.pack_into('>I', h, 0x06, len(node) // 2)
    struct.pack_into('>H', h, 0x0e, first)
    out = bytearray(h) + node
    rb = up(at + len(out))
    out += b'\0' * (rb - at - len(out))
    struct.pack_into('>I', out, 0x12, addr(rb))
    struct.pack_into('>I', out, 0x16, len(rows) * ADM_ROW_W)
    out += pack_rows(rows, deflate)
    dfo = up(at + len(out))
    out += b'\0' * (dfo - at - len(out))
    cmp_, blocks = pack_frame(names, ADM_PER, deflate)
    a = dfo + 0x100
    tail = up(a + len(cmp_))
    f = bytearray(df)
    struct.pack_into('>H', f, 0x30, max(len(r) for r in names) // 2)
    f[0x47] = max(r[2] for r in names)
    f[0x7f] = max(r[4] for r in names)
    struct.pack_into('>I', f, ADM_DF_FRAME[0], addr(a))
    struct.pack_into('>I', f, ADM_DF_FRAME[1], sum(b[1] for b in blocks))
    t = tail_table(blocks, ADM_PER)
    struct.pack_into('>IIH', f, ADM_DF_TAIL, addr(tail), len(t) // 2 - 2, ADM_PER)
    out += f + cmp_
    out += b'\0' * (tail - at - len(out))
    out += t
    out += b'\0' * (-len(out) % ALIGN)
    return bytes(out), at, dfo


def definition(df, names, infos):
    """The definition region with its content fields set; each formula holds on the eight
    POINT files measured (Andorra, Finland, Liechtenstein, Monaco, San Marino, Vatican and
    two more)."""
    f = bytearray(df)
    fields = [parse_name_record(r) for r in names]
    struct.pack_into('>H', f, 0x3a, max(len(r) for r in names) // 2)
    struct.pack_into('>H', f, 0x3c, 2 * (max(r[10] + r[11] for r in names) + 1) + 12)
    f[0x55] = len(fields[0]['levels'])
    struct.pack_into('>H', f, 0xd6, max(len(x['name']) + len(x['name']) % 2 for x in fields) // 2)
    struct.pack_into('>H', f, 0xf4, max(len(x['address']) + len(x['address']) % 2 for x in fields) // 2)
    struct.pack_into('>H', f, 0x148, max((len(r) for r in infos), default=0) // 2)
    f[0x17f] = max((r[3] for r in infos), default=0)
    return bytes(f)


def info_record(text):
    """One INFORMATION record: a telephone number as plain text."""
    t = text + b'\0' * (len(text) % 2)
    return struct.pack('>HBB', 2 + len(t) // 2, 0, len(t) // 2) + t


def assemble(p, deflate):
    """Bytes of a POINT file from a Point's content; every address and size is set here."""
    node, first, _ = build_trie(p.keys, CR, KW, ROW_W)
    out = bytearray(p.prefix)
    struct.pack_into('>I', out, 0x1006, len(node) // 2)
    struct.pack_into('>H', out, 0x100e, first)
    struct.pack_into('>I', out, 0x1016, len(p.rows) * ROW_W)
    out += node
    out += b'\0' * (-len(out) % ALIGN)
    struct.pack_into('>I', out, 0x1012, addr(len(out)))
    out += pack_rows(p.rows, deflate)
    out += b'\0' * (-len(out) % ALIGN)
    blob, s15 = near_frames(len(out), [(w[11], items, sub) for w, items, sub in p.near])
    struct.pack_into('>IH', out, 0x800 + 6 * 15, *s15)
    out += blob
    out += b'\0' * (-len(out) % ALIGN)
    for slot, groups, size in ((278, p.cats, CAT_ENTRY), (279, p.subs, SUB_ENTRY)):
        # no group leaves the slot empty (POINT032 has no sub-categories)
        blob, s = table_frames(len(out), groups, size, deflate) if groups else (b'', (0xffffffff, 0))
        struct.pack_into('>IH', out, 0x800 + 6 * slot, *s)
        out += blob
    dfr = len(out)
    struct.pack_into('>IH', out, 0x800 + 6 * 7, addr(dfr), DF_LEN // ALIGN)
    out += definition(p.df, p.names, p.infos)
    frames = []
    for recs, per, (fa, fw) in ((p.names, FAC_PER, DF_FAC), (p.infos, INF_PER, DF_INF)):
        cmp_, blocks = pack_frame(recs, per, deflate)
        struct.pack_into('>II', out, dfr + fa, addr(len(out)), sum(b[1] for b in blocks))
        out += cmp_
        out += b'\0' * (-len(out) % ALIGN)
        frames.append((blocks, per))
    for (blocks, per), ta in zip(frames, (DF_FAC_TAIL, DF_INF_TAIL)):
        t = tail_table(blocks, per)
        struct.pack_into('>IIH', out, dfr + ta, addr(len(out)), len(t) // 2 - 2, per)
        out += t
        out += b'\0' * (-len(out) % ALIGN)
    blob, h, dfo = adm_frame(len(out), p.adm_head, p.adm_df, p.adm_keys, p.adm_rows, p.adm_names, deflate)
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addr(dfo), 1)
    out += blob
    return bytes(out)
