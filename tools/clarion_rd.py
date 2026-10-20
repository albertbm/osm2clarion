#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Section 2 (road) records: node lists with per-node link entries.

Record: parcel code, two zero dwords, five unit pairs, then country codes up to where unit
0 starts (word 27, or 28 where a border crosses the parcel). Unit 0: node/link-end totals,
eleven (nodes, link ends) pairs per class group (rd_check.slot), then the nodes: entry count
and class in x/y's top nibbles, then per entry other node index, w4, w5, link id (0x2000 =
drawn last point), class word (far node's class high byte, link's class low nibble), travel
time, length, regulation words. An entry's class word with bit 15 set is a parcel-edge
connection; the top three bits give the side.
"""
import struct

def parse_record(pl):
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
    """(id, node a, node b, class word, a, b) for links seen from both ends."""
    ends = {}
    for i, nd in enumerate(rec['nodes']):
        for e in nd['entries']:
            if e['cls'] & 0x8000: continue
            ends.setdefault(e['id'] & 0x1fff, []).append((i, e))
    out = []
    for lid, lst in ends.items():
        if len(lst) == 2: out.append((lid, lst[0][0], lst[1][0], lst[0][1]['cls'], lst[0][1]['a'], lst[0][1]['b']))
    return out, ends
