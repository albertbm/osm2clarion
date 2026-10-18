#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The coarse map file LEV3456R.DAT (levels 6 to 3): read it into records, write it back.

The container is the tile's (header, section table at 0x800, per-section level-group
table), but its directories have their own shape. A directory is a 12-byte head (size in
halfwords, rows, cols, y, x, type) and then per row a u32 block address and P pairs of
(u32, u32), where P is cols or 2 * cols. A pair is (compressed words, inflated words) for a
zlib record, or the two part lengths in words of a raw record; ffffffff ffffffff is an
absent record. A row with no records has address ffffffff.

    level   rows x cols   pairs per row   head x
    6       1 x 1         1               x0
    5       11 x 11       11              x0 - 1024
    4       44 x 44       88 (sec 1), 44 (sec 2)
    3       16 x 16       32              x0

Layout, measured on the 2014, 2015 and 2016 stock files: section 1's table at 0x1000, then
per directory the directory and its row blocks in row order, records back to back inside a
block, then section 2 the same way. Every piece starts on a 256-byte boundary, the gaps are
zero, and the file ends at the next 256-byte boundary. Level 6's directory head gives its
size as 10 halfwords, not the 12 it spans. write() lays the file out from these
rules alone, so a byte-exact rebuild of a stock file proves the rules.

The header is tile_header's except seven bytes: a coarse file has 00ffffffffffff at 0x4ed
where a tile has 01011000000120.

    lev_file.py --check FILE...            rebuild each file and compare, records kept as stored
    lev_file.py --check --decode FILE...   the same with every record decoded and re-encoded
"""
import struct
import sys
import zlib

import tile_header
from clarion_tile import addr_of, enc_addr, pad256, walk

TABLE1 = 0x1000
COARSE = (0x4ed, bytes.fromhex('00ffffffffffff'))   # a tile has 01011000000120 here
ABSENT = 0xffffffff
ZLIB = b'\x58\x85'


def header(**fields):
    """A coarse header from the fields tile_header.author takes."""
    h = tile_header.author(**fields)
    off, b = COARSE
    h[off:off + len(b)] = b
    return h


def compress(plain):
    # memLevel 6 and a 4-byte pad rebuild every stock coarse stream; tile encoders use 8 and 2
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 6)
    out = c.compress(plain) + c.flush()
    return out + bytes(-len(out) % 4)


def zrec(plain):
    """A zlib record from its inflated bytes."""
    assert len(plain) % 2 == 0
    return dict(plain=plain)


def rawrec(a, b=b''):
    """A raw record of one or two parts."""
    assert len(a) % 2 == 0 and len(b) % 2 == 0
    return dict(raw=(a, b))


def rec_bytes(r):
    """(bytes, pair) for a record."""
    if 'plain' in r:
        z = r.get('z') or compress(r['plain'])
        return z, (len(z) // 2, len(r['plain']) // 2)
    a, b = r['raw']
    return a + b, (len(a) // 2, len(b) // 2)


def read(d, decode=False):
    """The file as {'fields', 'sections'}; a section is {'id', 'levels'}, a level
    {'level', 'typ', 'dirs'}, a directory {'box', 'cols', 'y', 'x', 'typ', 'pairs', 'rows'}
    and a row None or a list of records (None where absent). A zlib record keeps its stored
    stream as 'z' unless decode is set."""
    fields, sect = tile_header.fields(bytes(d[:0x1000]))
    levels, _ = walk(d, tile_header.LEVEL_OFF, 4)
    out = dict(fields=fields, sections=[])
    for (sid, lv), (ta, count) in zip(levels, sect):
        ta = addr_of(ta)
        nw = struct.unpack_from('>H', d, ta)[0]
        w = struct.unpack_from('>%dH' % nw, d, ta)
        sec = dict(id=sid, levels=[])
        for li, e in enumerate(lv):
            L = dict(level=e[0], typ=e[1], dirs=[])
            o = w[1 + li]
            if o != 0xffff:
                assert w[o] == 2 + 6 * w[o + 1]
                for g in range(w[o + 1]):
                    y1, y0, x0, x1, hi, lo = w[o + 2 + 6 * g:o + 8 + 6 * g]
                    L['dirs'].append(read_dir(d, addr_of(hi << 16 | lo), (y1, y0, x0, x1), decode))
            assert len(L['dirs']) == struct.unpack('>H', e[2:])[0]
            sec['levels'].append(L)
        out['sections'].append(sec)
    return out


def read_dir(d, da, box, decode):
    size, nrows, cols, y, x, typ = struct.unpack_from('>6H', d, da)
    pairs = 1 if nrows == cols == 1 else ((2 * size - 12) // nrows - 4) // 8
    assert head_size(nrows, cols, pairs) == size, (hex(da), size)
    D = dict(box=box, cols=cols, y=y, x=x, typ=typ, pairs=pairs, rows=[])
    q = da + 12
    for _ in range(nrows):
        tag = struct.unpack_from('>I', d, q)[0]
        pr = [struct.unpack_from('>II', d, q + 4 + 8 * k) for k in range(pairs)]
        if tag == ABSENT:
            D['rows'].append(None)          # stock rows are all ff; edited files leave pairs behind
        else:
            b, recs = addr_of(tag), []
            for ln, X in pr:
                if ln == ABSENT:
                    recs.append(None)
                elif d[b:b + 2] == ZLIB:
                    z = bytes(d[b:b + 2 * ln])
                    plain = zlib.decompress(z)
                    assert len(plain) == 2 * X
                    recs.append(dict(plain=plain) if decode else dict(plain=plain, z=z))
                    b += 2 * ln
                else:
                    recs.append(rawrec(bytes(d[b:b + 2 * ln]), bytes(d[b + 2 * ln:b + 2 * (ln + X)])))
                    b += 2 * (ln + X)
            D['rows'].append(recs)
        q += 4 + 8 * pairs
    return D


def table_words(sec, dir_addr):
    w = [0] * (1 + len(sec['levels']))
    for li, L in enumerate(sec['levels']):
        if not L['dirs']:
            w[1 + li] = 0xffff
            continue
        w[1 + li] = len(w)
        w += [2 + 6 * len(L['dirs']), len(L['dirs'])]
        for di, D in enumerate(L['dirs']):
            a = enc_addr(dir_addr[(li, di)]) if dir_addr else 0
            w += list(D['box']) + [a >> 16, a & 0xffff]
    w[0] = len(w)
    return w


def dir_size(D):
    return 12 + len(D['rows']) * (4 + 8 * D['pairs'])


def head_size(nrows, cols, pairs):
    # level 6's one-pair directory says 10 halfwords where it spans 12, in all three stock files
    return 10 if nrows == cols == 1 else (12 + nrows * (4 + 8 * pairs)) // 2


def write(lev):
    """The file's bytes, laid out by the stock rules."""
    out = bytearray(TABLE1)

    def put(b):
        out.extend(b)
        out.extend(bytes(pad256(len(out)) - len(out)))

    sect = []
    for sec in lev['sections']:
        ta = len(out)
        nw = len(table_words(sec, None))
        put(bytes(2 * nw))
        sect.append((enc_addr(ta), -(-2 * nw // 256)))
        addrs = {}
        for li, L in enumerate(sec['levels']):
            for di, D in enumerate(L['dirs']):
                da = addrs[(li, di)] = len(out)
                put(bytes(dir_size(D)))
                head = bytearray(struct.pack('>6H', head_size(len(D['rows']), D['cols'], D['pairs']), len(D['rows']), D['cols'],
                                             D['y'], D['x'], D['typ']))
                for recs in D['rows']:
                    if recs is None:
                        head += b'\xff' * (4 + 8 * D['pairs'])
                        continue
                    assert len(recs) == D['pairs']
                    head += struct.pack('>I', enc_addr(len(out)))
                    block = bytearray()
                    for r in recs:
                        if r is None:
                            head += b'\xff' * 8
                            continue
                        b, pair = rec_bytes(r)
                        head += struct.pack('>II', *pair)
                        block += b
                    put(block)
                out[da:da + len(head)] = head
        out[ta:ta + 2 * nw] = struct.pack('>%dH' % nw, *table_words(sec, addrs))
    f = dict(lev['fields'])
    f['level_groups'] = [(sec['id'], [(L['level'], L['typ'], len(L['dirs'])) for L in sec['levels']])
                         for sec in lev['sections']]
    h = header(**f)
    for i, (a, c) in enumerate(sect):
        struct.pack_into('>IH', h, tile_header.SECT_OFF + 6 * i, a, c)
    out[:TABLE1] = h
    return bytes(out)


def first_diff(a, b):
    n = min(len(a), len(b))
    return next((i for i in range(n) if a[i] != b[i]), n)


if __name__ == '__main__':
    args = sys.argv[1:]
    if args[:1] != ['--check']:
        sys.exit(__doc__)
    decode = '--decode' in args
    files = [a for a in args[1:] if a != '--decode']
    bad = 0
    for fn in files:
        d = open(fn, 'rb').read()
        lev = read(d, decode)
        b = write(lev)
        nrec = sum(r is not None for sec in lev['sections'] for L in sec['levels']
                   for D in L['dirs'] for row in D['rows'] if row for r in row)
        if b == d:
            print(fn, 'byte-identical', len(d), 'B,', nrec, 'records')
        else:
            bad += 1
            i = first_diff(b, d)
            print(fn, 'DIFFERS: len %d vs %d, first at %#x' % (len(d), len(b), i), d[i:i + 8].hex(), b[i:i + 8].hex())
    sys.exit(1 if bad else 0)
