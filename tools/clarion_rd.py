#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Section 2 (road) records: node lists with per-node link entries.

Record: words 0-1 parcel code, two zero dwords, five (u32 offset, u32 length) unit pairs in
16-bit words from the record start, then from word 26 the ISO 3166 numeric country codes of
the record, one word each, up to where unit 0 starts (word 27 for one country, 28 for a
record a border runs through: 578 then 752 for Norway and Sweden). Unit 0 is the graph:
u32 nodes, u32 link ends, then eleven (u16 nodes, u16 link ends) pairs at u4..u25 per class
group (rd_check.slot), then the nodes from u26. A node is x (entry count in the top nibble),
y (the node's class in the top nibble), a flag word, then its entries. Entries whose class
word has bit 15 set are parcel-edge connections; the top three bits give the side.
"""
import struct

def parse_record(pl):
    """Node list of unit 0. Node: x (entry count in the top nibble), y (node class in the top
    nibble), a node flag word, then per entry: other node index, w4, w5, link id (0x2000 = this
    is the northern end), class word (the far node's class in the high byte, the link's class in
    the low nibble), travel time, length, then extra words: (w5 >> 6) & 0x3f regulation words
    plus w5 & 0xf more. Until 18 Oct 2026 this read four bits and no low count, and 142 of
    22,981 records in the Denmark tile desynchronised; this form parses all of them. A class word with bit 15 set is a parcel-edge
    connection."""
    w = list(struct.unpack('>%dH' % (len(pl) // 2), pl[:len(pl) // 2 * 2]))
    code = (w[0], w[1])
    pairs = [struct.unpack_from('>II', pl, 12 + 8 * i) for i in range(5)]
    units = {i: w[o:o + n] for i, (o, n) in enumerate(pairs) if o != 0xffffffff}
    out = dict(code=code, pairs=pairs, units=units, nodes=[], trailer=w[27 + sum(n for o, n in pairs if o != 0xffffffff):])
    if 0 not in units: return out
    u = units[0]
    out['nnodes'] = (u[0] << 16) | u[1]; out['nlinks'] = (u[2] << 16) | u[3]
    out['classes'] = [(u[4 + 2 * i], u[5 + 2 * i]) for i in range(11)]
    out['countries'] = w[26:pairs[0][0]]
    p = 26
    while p + 9 <= len(u):
        x, y, nflags = u[p], u[p + 1], u[p + 2]; cnt = x >> 12
        node = dict(x=x & 0x7ff, y=y & 0x7ff, xflags=x & 0x0800, yflags=y >> 11, ncls=y >> 12, nflags=nflags, entries=[], at=p)
        p += 3
        for ei in range(cnt):
            if p + 7 > len(u): out['error'] = f'entry runs past the unit at node {len(out["nodes"])}'; break
            other, w4, w5 = u[p:p + 3]; p += 3
            eid, cls, a, b = u[p:p + 4]; p += 4
            extra = ((w5 >> 6) & 0x3f) + (w5 & 0xf)
            node['entries'].append(dict(other=other, w4=w4, w5=w5, id=eid, cls=cls, a=a, b=b, extra=u[p:p + extra])); p += extra
        out['nodes'].append(node)
        if 'error' in out: break
    out['consumed'] = p; out['unit_len'] = len(u)
    return out

def links(rec):
    """(id, node index a, node index b, class word, a, b) for links seen from both ends."""
    ends = {}
    for i, nd in enumerate(rec['nodes']):
        for e in nd['entries']:
            if e['cls'] & 0x8000: continue
            ends.setdefault(e['id'] & 0x1fff, []).append((i, e))
    out = []
    for lid, lst in ends.items():
        if len(lst) == 2: out.append((lid, lst[0][0], lst[1][0], lst[0][1]['cls'], lst[0][1]['a'], lst[0][1]['b']))
    return out, ends
