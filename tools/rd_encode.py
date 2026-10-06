#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Encode a section-2 (road) level-1 record and directory from a node/link graph.

Graph: nodes [(x, y)] in parcel units, links [(i, j, length_m)]. Follows the shipped layout
read by clarion_rd: nodes sorted by x, entries ordered as given, no parcel-edge connections.
"""
import math, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, fa_encode

CLASS = 0x0898

def class_word(cls):
    """The link class word: class in the high byte, then 0x90 | class. Measured over the 2014
    card's south-west Norway tile, where the common words are 0x898 (class 8, 102,045 links),
    0x595, 0x999, 0x494, 0x393, 0x797. The two constants this file used are that rule at
    classes 8 and 7. Classes above 9 ship as 0x80 | class instead (0xa8a, 0xc8c)."""
    return cls << 8 | (0x90 if cls <= 9 else 0x80) | cls

def build_record(level, y0, x0, row, col, nodes, links, compress=True):
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    for lid, (i, j, length) in enumerate(links, start=1):
        yi, yj = nodes[i][1], nodes[j][1]
        north = i if (yi, nodes[i][0]) > (yj, nodes[j][0]) else j
        a = 0x8000 | min(0x3ff, round(0.4 * length))
        for end, other in ((i, j), (j, i)):
            adj[end].append((pos[other], lid | (0x2000 if end == north else 0), a, length))
    body = []
    for old in order:
        x, y = nodes[old]; ents = adj[old]
        assert ents, 'isolated node'
        assert len(ents) <= 15, 'node %r has %d entries, too many for the count nibble' % (nodes[old], len(ents))
        body += [(len(ents) << 12) | x, 0x8000 | y, 0]
        for other, lid, a, length in ents:
            body += [other, 0, 0, lid, CLASS, a, min(0xffff, round(length))]
    nn, nl = len(nodes), 2 * len(links)
    unit = [nn >> 16, nn & 0xffff, nl >> 16, nl & 0xffff, 0, 0, 0, 0, 0, 0, nn, nl] + [0] * 14 + body
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]   # parcels per mesh
    hdr = list(clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)) + [0, 0, 0, 0]
    pairs = [(0x1b, len(unit))] + [(0xffffffff, 0)] * 4
    out = struct.pack('>6H', *hdr)
    for off, ln in pairs: out += struct.pack('>II', off, ln)
    out += struct.pack('>H', 0x017c)
    assert len(out) == 54
    out += struct.pack('>%dH' % len(unit), *unit)
    if not compress: return out, out          # level-2 records ship raw
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(out) + c.flush()
    if len(comp) % 2: comp += b'\0'
    return out, comp

SIDE_B = {0x8000: 0x8000, 0xa000: 0x0080, 0xc000: 0xc000, 0xe000: 0x00c0}   # part-b side flags (top, right, bottom, left)

def build_record_edges(level, y0, x0, row, col, nodes, links, edges, compress=True, classes=None):
    """build_record with parcel-edge connections: edges = {node index: (side bits, twin index)}.
    Level 1: zlib, header tail 0x017c. Level 2: raw two parts, header tail 0x00fa, second part
    = per-node connection table. classes: per-link OSM highway value; each link then gets its
    own class word instead of the level's default. Routing reads this, the renderer does not --
    rewriting all 160,876 level-1 link words in a tile changed nothing on screen."""
    default = 0x0898 if level == 1 else 0x0797
    cls_of = [class_word(fa_encode.layer_class(hw, level)[1]) for hw in classes] if classes else None
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    for lid, (i, j, length) in enumerate(links, start=1):
        yi, yj = nodes[i][1], nodes[j][1]
        north = i if (yi, nodes[i][0]) > (yj, nodes[j][0]) else j
        a = 0x8000 | min(0x3ff, round(0.4 * length))
        cw = cls_of[lid - 1] if cls_of else default
        for end, other in ((i, j), (j, i)):
            adj[end].append((pos[other], lid | (0x2000 if end == north else 0), cw, a, min(0xffff, round(length))))
    for i, (side, twin) in edges.items():
        adj[i].append((twin, 0, side | default, 0x8000, 0))
    body = []
    for old in order:
        x, y = nodes[old]; ents = adj[old]
        if not ents: continue
        # the entry count shares its word with x, so 15 is the ceiling; above it x is corrupted
        assert len(ents) <= 15, 'node %r has %d entries, too many for the count nibble' % (nodes[old], len(ents))
        body += [(len(ents) << 12) | x, 0x8000 | y, 0]
        for other, lid, cls, a, length in ents: body += [other, 0, 0, lid, cls, a, length]
    nn = sum(1 for old in order if adj[old]); nl = sum(len(v) for v in adj.values())
    unit = [nn >> 16, nn & 0xffff, nl >> 16, nl & 0xffff, 0, 0, 0, 0, 0, 0, nn, nl] + [0] * 14 + body
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]
    hdr = list(clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)) + [0, 0, 0, 0]
    tail = 0x017c if level == 1 else 0x00fa
    out = struct.pack('>6H', *hdr)
    for off, ln in [(0x1b, len(unit))] + [(0xffffffff, 0)] * 4: out += struct.pack('>II', off, ln)
    out += struct.pack('>H', tail) + struct.pack('>%dH' % len(unit), *unit)
    if level == 1:
        c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(out) + c.flush()
        if len(comp) % 2: comp += b'\0'
        return out, comp
    # part b: connection table. Header nodes, 0xa100; per node 2000 k 0 k then per entry (other, side, other)
    pb = [nn, 0xa100]
    for k, old in enumerate(o for o in order if adj[o]):
        pb += [0x2000, k, 0, k]
        for other, lid, cls, a, length in adj[old]:
            pb += [other, SIDE_B.get(cls & 0xe000, 0) if cls & 0x8000 else 0, other]
    pb += [hdr[0], hdr[1], 0]
    partb = struct.pack('>6H', *hdr)
    for off, ln in [(0xffffffff, 0), (0x1b, len(pb))] + [(0xffffffff, 0)] * 3: partb += struct.pack('>II', off, ln)
    partb += struct.pack('>H', tail) + struct.pack('>%dH' % len(pb), *pb)
    return out, partb   # caller stores the pair as (part a words, part b words)

def make_dir(level, y0, x0, recs):
    """Section-2 directory: kind 0x1046, one pair per column, 32 slots of parcel rows."""
    typ = {1: 0x90, 2: 0x81}[level]
    body = 256; npairs = body // 8
    slots = {}
    for row, cols in sorted(recs.items()):
        pairs = [b'\xff' * 8] * npairs; rlist = []
        for col, (plain, comp) in sorted(cols.items()):
            if level == 1:   # zlib: (compressed words, inflated words)
                pairs[col] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
                rlist.append(dict(k=col, raw=comp, words=len(plain) // 2))
            else:            # raw: (part a words, part b words), stored back to back
                pairs[col] = struct.pack('>II', len(plain) // 2, len(comp) // 2)
                rlist.append(dict(k=col, raw=plain + comp, words=len(comp) // 2))
        slots[row] = dict(body=b''.join(pairs), recs=rlist)
    n = {1: 1, 2: 4}[level]
    return dict(kind=0x1046, ab=(32, 32), y=y0, x=x0, typ=typ, body=body, nslots=32, slots=slots,
                empty_bodies={i: b'\xff' * body for i in range(32) if i not in slots}, box=(y0 + n, y0, x0, x0 + n))
