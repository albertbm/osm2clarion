# SPDX-License-Identifier: GPL-3.0-or-later
"""Clarion X08-03-00 map tile: read the container into objects and write it back.

Layout: 0x1000-byte header (strings, level table at 0x1ac, bbox table at 0x2ac,
section table at 0x800), then per section a section table, directories and record
blocks. Everything after the header is 256-byte aligned. Addresses are u32:
(offset // 2048) << 8 | (offset % 2048) // 256.
"""
import struct, zlib

SLOTS = 32
LEVEL_OFF, BBOX_OFF, SECT_OFF, TABLE1_OFF = 0x1ac, 0x2ac, 0x800, 0x1000

def addr_of(v): return (v >> 8) * 2048 + (v & 0xff) * 256
def enc_addr(off):
    assert off % 256 == 0
    return (off // 2048) << 8 | (off % 2048) // 256
def pad256(n): return (n + 255) // 256 * 256

def walk(d, off, elen):
    n = struct.unpack_from('>H', d, off)[0]; p = off + 2; out = []
    for _ in range(n):
        sid, cnt = d[p], d[p+1]; p += 2
        out.append((sid, [d[p+i*elen:p+(i+1)*elen] for i in range(cnt)])); p += cnt*elen
    return out, p

def read_tile(d):
    t = dict(header=bytearray(d[:TABLE1_OFF]), sections=[], order=[])
    levels, _ = walk(d, LEVEL_OFF, 4)
    sect_entries = []
    p = SECT_OFF
    while True:
        a, c = struct.unpack_from('>IH', d, p); p += 6
        if a == 0xffffffff: break
        sect_entries.append((addr_of(a), c))
    for (sid, lv), (taddr, count) in zip(levels, sect_entries):
        nlev = len(lv)
        nw = struct.unpack_from('>H', d, taddr)[0]
        w = struct.unpack_from('>%dH' % nw, d, taddr)
        sec = dict(id=sid, count=count, taddr=taddr, levels=[], table_words=nw)
        si = len(t['sections']); t['order'].append((taddr, 'table', si))
        for li, e in enumerate(lv):
            level, typ, cnt = e[0], e[1], struct.unpack('>H', e[2:])[0]
            o = w[1+li]
            L = dict(level=level, typ=typ, dirs=[])
            if o != 0xffff:
                glen, gcnt = w[o], w[o+1]
                assert glen == 2 + 6*gcnt and gcnt == cnt, (glen, gcnt, cnt)
                for k in range(gcnt):
                    y1, y0, x0, x1, z, tg = w[o+2+6*k:o+8+6*k]
                    D = read_dir(d, addr_of((z << 16) | tg), box=(y1, y0, x0, x1), nslots=16 if level == 3 else SLOTS)
                    di = len(L['dirs']); L['dirs'].append(D)
                    t['order'].append((D['off'], 'dir', si, li, di))
                    if D['slots']:
                        t['order'].append((min(s['block_off'] for s in D['slots'].values()), 'blocks', si, li, di))
            else:
                assert cnt == 0
            sec['levels'].append(L)
        t['sections'].append(sec)
    t['order'].sort()
    return t

def read_dir(d, off, box, nslots=SLOTS):
    kind, a, b, y, x, typ = struct.unpack_from('>HHHHHH', d, off)
    body = 512 if kind == 0x2046 else 256
    D = dict(off=off, kind=kind, ab=(a, b), y=y, x=x, typ=typ, box=box, body=body, slots={}, nslots=nslots)
    p = off + 12
    for i in range(nslots):
        tag = struct.unpack_from('>I', d, p)[0]
        raw = d[p+4:p+4+body]
        if tag != 0xffffffff:
            words = struct.unpack_from('>%dI' % (body//4), raw)
            recs = []
            q = addr_of(tag)
            for k in range(body//8):
                ln, X = words[2*k], words[2*k+1]
                if ln in (0xffffffff, 0x7fffffff): continue
                # zlib records: ln = compressed words, X = inflated words.
                # raw records (no 58 85 magic): two parts of ln and X words.
                n = 2*ln if d[q:q+2] == b'\x58\x85' else 2*(ln + X)
                recs.append(dict(k=k, raw=d[q:q+n], words=X)); q += n
            D['slots'][i] = dict(tag=tag, body=raw, recs=recs, block_off=addr_of(tag), block_end=q)
        else:
            D.setdefault('empty_bodies', {})[i] = raw
        p += 4 + body
    D['end'] = p
    return D

def dir_bytes(D, slot_addrs):
    out = bytearray(struct.pack('>HHHHHH', D['kind'], *D['ab'], D['y'], D['x'], D['typ']))
    for i in range(D.get('nslots', SLOTS)):
        s = D['slots'].get(i)
        if s is None:
            out += b'\xff' * 4 + D.get('empty_bodies', {}).get(i, b'\xff' * D['body']); continue
        out += struct.pack('>I', enc_addr(slot_addrs[i])) + s['body']
    return bytes(out)

def block_bytes(s):
    return b''.join(r['raw'] for r in s['recs'])

def table_words(sec):
    lv_offs = []; groups = []
    pos = 1 + len(sec['levels'])
    for L in sec['levels']:
        if not L['dirs']: lv_offs.append(0xffff); continue
        lv_offs.append(pos); groups.append(L); pos += 2 + 6*len(L['dirs'])
    return pos, lv_offs, groups

def default_order(t):
    """Layout for a new tile: per section, table, then per level its dirs, then its blocks."""
    order = []
    for si, sec in enumerate(t['sections']):
        order.append((0, 'table', si))
        for li, L in enumerate(sec['levels']):
            for di in range(len(L['dirs'])): order.append((0, 'dir', si, li, di))
            for di, D in enumerate(L['dirs']):
                if D['slots']: order.append((0, 'blocks', si, li, di))
    return order

def write_tile(t, order=None):
    order = order or t.get('order') or default_order(t)
    # pass 1: sizes and positions
    pos = TABLE1_OFF; where = {}
    for o in order:
        kind = o[1]
        if kind == 'table':
            sec = t['sections'][o[2]]; nw, _, _ = table_words(sec)
            where[o[1:]] = pos; pos = pad256(pos + 2*nw)
        elif kind == 'dir':
            D = t['sections'][o[2]]['levels'][o[3]]['dirs'][o[4]]
            where[o[1:]] = pos; pos = pad256(pos + 12 + D.get('nslots', SLOTS)*(4 + D['body']))
        else:
            D = t['sections'][o[2]]['levels'][o[3]]['dirs'][o[4]]
            for i in sorted(D['slots']):
                where[('slot',) + o[2:] + (i,)] = pos
                pos = pad256(pos + len(block_bytes(D['slots'][i])))
    out = bytearray(t['header']); assert len(out) == TABLE1_OFF
    for o in order:
        kind = o[1]; p = where[o[1:]] if kind != 'blocks' else None
        if kind == 'table':
            si = o[2]; sec = t['sections'][si]; nw, lv_offs, groups = table_words(sec)
            w = [nw] + lv_offs
            for L in groups:
                li = sec['levels'].index(L)
                w += [2 + 6*len(L['dirs']), len(L['dirs'])]
                for di, D in enumerate(L['dirs']):
                    a = enc_addr(where[('dir', si, li, di)])
                    w += [D['box'][0], D['box'][1], D['box'][2], D['box'][3], a >> 16, a & 0xffff]
            assert len(out) == p
            out += struct.pack('>%dH' % nw, *w)
        elif kind == 'dir':
            si, li, di = o[2:]; D = t['sections'][si]['levels'][li]['dirs'][di]
            assert len(out) == p
            out += dir_bytes(D, {i: where[('slot', si, li, di, i)] for i in D['slots']})
        else:
            si, li, di = o[2:]; D = t['sections'][si]['levels'][li]['dirs'][di]
            for i in sorted(D['slots']):
                assert len(out) == where[('slot', si, li, di, i)]
                out += block_bytes(D['slots'][i]); out += b'\0' * (pad256(len(out)) - len(out))
        out += b'\0' * (pad256(len(out)) - len(out))
    p = SECT_OFF
    for si, sec in enumerate(t['sections']):
        struct.pack_into('>IH', out, p, enc_addr(where[('table', si)]), sec['count']); p += 6
    return bytes(out)

if __name__ == '__main__':
    import sys
    for fn in sys.argv[1:]:
        d = open(fn, 'rb').read()
        t = read_tile(d)
        b = write_tile(t)
        if b == d: print(fn, 'byte-identical rebuild OK', len(d))
        else:
            i = next((i for i in range(min(len(b), len(d))) if b[i] != d[i]), min(len(b), len(d)))
            print(fn, 'MISMATCH len', len(d), len(b), 'first diff', hex(i), d[i:i+16].hex(), b[i:i+16].hex())
