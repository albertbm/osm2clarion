# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write the Clarion ADDRE street-search family.

Decompressed form. Offsets and sizes inside the trie are 16-bit words; so are the
name-block chunk sizes in the tail table, which is what made the block look like an
unknown codec. The name blocks are zlib with a 4 KiB window.
"""
import struct
import zlib

ALIGN = 0x100
DF_LEN = 0x100
NAMES_PER_BLOCK = 500
SREC = 22
SREC_W = 11


def u16(d, o): return struct.unpack_from('>H', d, o)[0]
def u32(d, o): return struct.unpack_from('>I', d, o)[0]


def addr(off):
    """KIWI 1.2.7 sector address: 2 KiB sector in the high bits, 256-byte block in the low 3."""
    assert off % ALIGN == 0, off
    return (off // 0x800) << 8 | (off % 0x800) // ALIGN


def inflate_block(d, off, cw):
    return zlib.decompress(d[off:off + cw * 2])


def deflate_block(b):
    co = zlib.compressobj(6, zlib.DEFLATED, 13, 6)
    return co.compress(b) + co.flush()


class Addre:
    """Parsed file. `keys` and `names` are the content; everything else is derived."""

    def __init__(self, d):
        self.d = d
        self.cr = d[0x1032] * 2
        self.kw = d[0x1033]
        self.area = u16(d, 0x103a)
        self.nodew = u32(d, 0x1006)
        self.node_end = 0x1100 + self.nodew * 2
        self.nsrec = u32(d, 0x1016) // SREC_W
        self.srec_off = d.find(b'\x7f\x7f', self.node_end)
        self.srec_pre = d[(self.srec_off // ALIGN) * ALIGN:self.srec_off]
        self.srec_end = self.srec_off + self.nsrec * SREC
        self.nb = (self.srec_end + DF_LEN + ALIGN - 1) // ALIGN * ALIGN
        self.df = d[self.nb - DF_LEN:self.nb]
        self.ntier = self.df[0x49] - 2  # definition frame +0x29; exact on all 7 factory files
        self._tail()
        self._trie()

    def _tail(self):
        d = self.d
        t = self.nb
        while d[t:t + 4] != b'\x01\xf4\x00\x00':
            t = (t // ALIGN + 1) * ALIGN
        self.tail_off = t
        self.blocks = []
        o = t + 4
        while True:
            st, cw, uw = struct.unpack_from('>III', d, o)
            if cw == 0:
                break
            self.blocks.append((st, cw, uw))
            o += 12
        raw = b''.join(inflate_block(d, self.nb + st * 2, cw) for st, cw, uw in self.blocks)
        self.names = []
        q = 0
        while q < len(raw):
            L = (u16(raw, q) & 0x7ff) * 2
            if L == 0:
                break
            self.names.append(raw[q:q + L])
            q += L

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
                keys.append((k, c[0] // SREC_W, c[3]))
                continue
            fo = 0x1100 + c[0] * 2
            n = c[1] * 2 // self.cr - 1
            own = d[fo + n * self.cr + 8 + self.cr - 9]
            if own:
                keys.append((k, u32(d, fo + n * self.cr) // SREC_W, own))
            for i in range(n):
                stack.append((self._crec(fo + i * self.cr), k))
        keys.sort()
        self.keys = keys

    def srec(self, i):
        return self.d[self.srec_off + i * SREC:self.srec_off + (i + 1) * SREC]


def build_trie(keys, cr, kw, recw=SREC_W):
    """keys: sorted [(key bytes, first street-record index, count)]."""
    class N:
        __slots__ = ('cnt', 'first', 'kids', 'off', 'own', 'size')

        def __init__(self):
            self.kids = {}
            self.own = 0
            self.first = None
            self.cnt = 0
    root = N()
    for k, idx, cnt in keys:
        n = root
        for i in range(0, len(k), kw):
            n.cnt += cnt
            if n.first is None:
                n.first = idx
            n = n.kids.setdefault(k[i:i + kw], N())
        n.cnt += cnt
        if n.first is None:
            n.first = idx
        n.own += cnt
    order = []

    def visit(n):
        order.append(n)
        for u in sorted(n.kids):
            visit(n.kids[u])
    for u in sorted(root.kids):
        visit(root.kids[u])
    pos = (len(root.kids) * cr + cr + 3) // 4 * 4
    first = pos // 2
    for n in order:
        if not n.kids:
            continue
        n.off = pos // 2
        n.size = (len(n.kids) + 1) * cr // 2
        pos = (pos + n.size * 2 + 3) // 4 * 4
    buf = bytearray(pos)

    def crec(o, n, key):
        if n.kids:
            struct.pack_into('>IHH', buf, o, n.off, n.size, 0)
        else:
            struct.pack_into('>IHH', buf, o, n.first * recw, 0, 0)
        buf[o + 8:o + 8 + kw] = key
        buf[o + 8 + kw:o + cr] = n.cnt.to_bytes(cr - 8 - kw, 'big')

    def tail(o, n, parent_off):
        struct.pack_into('>II', buf, o, n.first * recw, parent_off)
        pad = bytearray(cr - 8)
        pad[:kw] = b'\xff' * kw
        pad[-1] = n.own
        buf[o + 8:o + 8 + cr - 8] = pad

    def emit(n, parent_off):
        o = n.off * 2
        ks = sorted(n.kids)
        for i, u in enumerate(ks):
            crec(o + i * cr, n.kids[u], u)
        tail(o + len(ks) * cr, n, parent_off)
        for u in ks:
            if n.kids[u].kids:
                emit(n.kids[u], n.off)
    ks = sorted(root.kids)
    for i, u in enumerate(ks):
        crec(i * cr, root.kids[u], u)
    struct.pack_into('>II', buf, len(ks) * cr, 0, 0xffffffff)
    pad = bytearray(cr - 8)
    pad[:kw] = b'\xff' * kw
    pad[-1] = root.own
    buf[len(ks) * cr + 8:len(ks) * cr + 8 + cr - 8] = pad
    for u in ks:
        if root.kids[u].kids:
            emit(root.kids[u], 0)
    return bytes(buf), first, len(ks)


def assemble(prefix, keys, srecs, names, df, srec_pre, cr, kw, area, ntier,
             maxkey_hint=None, namewidth_hint=None):
    """prefix: bytes 0..0x1100 from a donor; patched in place. Returns (file, blocks).

    Fills every definition-frame field that has a known formula (REPORT-addre-fields.md,
    verified exact on all seven factory ADDRE files). D and the name-width hint have
    none, so they pass through from `df` unless `maxkey_hint`/`namewidth_hint` override
    them - needed whenever `df` is a donor's, not this content's own, as for Iceland.
    """
    node, first, _nroot = build_trie(keys, cr, kw)
    out = bytearray(prefix)
    out[0x1032] = cr // 2
    out[0x1033] = kw
    struct.pack_into('>H', out, 0x103a, area)
    struct.pack_into('>I', out, 0x1006, len(node) // 2)
    struct.pack_into('>H', out, 0x100e, first)
    struct.pack_into('>I', out, 0x1016, len(srecs) * SREC_W)
    struct.pack_into('>H', out, 0x101a, SREC_W)
    out += node
    srb = len(out) + (-len(out)) % ALIGN             # street-record block, 256-aligned
    struct.pack_into('>I', out, 0x1012, addr(srb))
    out += b'\x00' * (srb - len(out))
    out += srec_pre
    for s in srecs:
        out += s
    nb = (len(out) + DF_LEN + ALIGN - 1) // ALIGN * ALIGN
    out += b'\x00' * (nb - DF_LEN - len(out))
    out += df
    assert len(out) == nb
    dfo = nb - DF_LEN + 0x20          # the definition frame proper, inside the definition region
    blocks = []
    cmp_parts = []
    st = 0
    for i in range(0, len(names), NAMES_PER_BLOCK):
        plain = b''.join(names[i:i + NAMES_PER_BLOCK])
        c = deflate_block(plain)
        c += b'\x00' * (-len(c) % 4)
        blocks.append((st, len(c) // 2, len(plain) // 2))
        cmp_parts.append(c)
        st += len(c) // 2
    out += b''.join(cmp_parts)
    tail = (len(out) + ALIGN - 1) // ALIGN * ALIGN
    out += b'\x00' * (tail - len(out))
    t = bytearray((4 + 12 * len(blocks) + ALIGN - 1) // ALIGN * ALIGN)
    struct.pack_into('>I', t, 0, 0x01f40000)
    o = 4
    for b in blocks:
        struct.pack_into('>III', t, o, *b)
        o += 12
    out += t

    cmpw = sum(b[1] for b in blocks)
    struct.pack_into('>I', out, dfo + 4, addr(nb))                           # A: name-block address
    struct.pack_into('>I', out, dfo + 8, cmpw)                               # B: total compressed words
    struct.pack_into('>H', out, dfo + 16, max(len(n) for n in names) // 2)    # C: longest name, words
    if maxkey_hint is not None:
        struct.pack_into('>H', out, dfo + 18, maxkey_hint)                   # D
    out[dfo + 0x29] = 2 + ntier
    if namewidth_hint is not None:
        out[dfo + 0x43] = namewidth_hint                                     # the name-width hint
    struct.pack_into('>I', out, dfo + 0x5a, addr(tail))                      # P: tail-table address
    struct.pack_into('>I', out, dfo + 0x5e, (4 + 12 * len(blocks)) // 2)      # Q: tail words
    # 0x82a is addr(nb - 0x100), the definition region's own address, not A - 1 -
    # that held by coincidence on small files and breaks on Germany
    struct.pack_into('>I', out, 0x824 + 6, addr(nb - ALIGN))                  # slot 7
    return bytes(out), blocks
