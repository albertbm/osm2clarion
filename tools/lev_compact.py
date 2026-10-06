#!/usr/bin/env python3
"""Rewrite LEV3456R.DAT with only the referenced pieces: header, section tables, directories
and their record blocks, in that order per section. Every edit tool appends, so the file
grows; this puts it back to size.

    ./lev_compact.py IN.DAT OUT.DAT
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clarion_tile import addr_of, enc_addr, pad256

NLEVELS = {0: 6, 1: 4, 2: 4}   # level slots per section table (levels 6..1; 5,4,2,1)

def dir_shape(kind, cols, rows, level_slot, sec):
    """(body bytes, records per column) for a directory."""
    if kind == 0x0826: return 256, 2                 # level-3 cell
    if sec == 1 and level_slot == 1: return 8, 1     # level 6: one slot, one pair
    if sec == 1 and level_slot == 3: return cols * 16, 2   # level 4: two pairs per column
    return cols * 8, 1                               # level 5 and section-2 directories

def read_dir(d, a, level_slot, sec):
    kind, cols, rows, y, x, typ = struct.unpack_from('>6H', d, a)
    body, ppc = dir_shape(kind, cols, rows, level_slot, sec)
    hdr = bytes(d[a:a + 12]); slots = []; p = a + 12
    for r in range(rows):
        tag = struct.unpack_from('>I', d, p)[0]; pairs = bytes(d[p + 4:p + 4 + body])
        block = None
        if tag != 0xffffffff:
            q = addr_of(tag); n = 0
            for k in range(body // 8):
                ln, X = struct.unpack_from('>II', pairs, 8 * k)
                if ln in (0xffffffff, 0x7fffffff): continue
                z = d[q + n:q + n + 2] == b'\x58\x85'; n += 2 * ln if z else 2 * (ln + X)
            block = bytes(d[q:q + n])
        slots.append((tag, pairs, block)); p += 4 + body
    return dict(hdr=hdr, slots=slots, size=12 + rows * (4 + body))

def main():
    src, dest = sys.argv[1:3]
    d = open(src, 'rb').read()
    out = bytearray(d[:0x1000])
    sections = []
    for i in range(4):
        a, c = struct.unpack_from('>IH', d, 0x800 + 6 * i)
        if a in (0, 0xffffffff): break
        sections.append((addr_of(a), c))
    kept = 0
    for si, (ta, count) in enumerate(sections):
        nw = struct.unpack_from('>H', d, ta)[0]; w = list(struct.unpack_from('>%dH' % nw, d, ta))
        # directories per level group
        dirs = []
        for slot in range(1, 1 + NLEVELS[si]):
            if w[slot] == 0xffff: continue
            o = w[slot]; cnt = w[o + 1]
            for i in range(cnt):
                z, t = w[o + 6 + 6 * i], w[o + 7 + 6 * i]
                dirs.append((slot, i, o + 6 + 6 * i, read_dir(d, addr_of((z << 16) | t), slot, si + 1)))
        # lay out: table, then dirs, then blocks
        tpos = pad256(len(out)); out += b'\0' * (tpos - len(out)); out += b'\0' * (2 * nw); out += b'\0' * (pad256(len(out)) - len(out))
        for slot, i, wi, D in dirs:
            dpos = pad256(len(out)); out += b'\0' * (dpos - len(out)); D['pos'] = dpos; out += b'\0' * D['size']; out += b'\0' * (pad256(len(out)) - len(out))
        for slot, i, wi, D in dirs:
            tags = []
            for tag, pairs, block in D['slots']:
                if block is None: tags.append(tag); continue
                bpos = pad256(len(out)); out += b'\0' * (bpos - len(out)); out += block; out += b'\0' * (pad256(len(out)) - len(out)); tags.append(enc_addr(bpos)); kept += 1
            body = bytearray(D['hdr']); 
            for (tag, pairs, block), nt in zip(D['slots'], tags): body += struct.pack('>I', nt) + pairs
            out[D['pos']:D['pos'] + len(body)] = body
            a = enc_addr(D['pos']); w[wi], w[wi + 1] = a >> 16, a & 0xffff
        struct.pack_into('>%dH' % nw, out, tpos, *w)
        struct.pack_into('>IH', out, 0x800 + 6 * si, enc_addr(tpos), count)
        print(f'section {si + 1}: table {nw} words at {tpos:#x}, {len(dirs)} directories')
    open(dest, 'wb').write(out)
    print(f'{len(d)} -> {len(out)} bytes, {kept} blocks')

if __name__ == '__main__':
    main()
