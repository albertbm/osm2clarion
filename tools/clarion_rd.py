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
    unit_end = max((o + n for o, n in pairs if o != 0xffffffff), default=27)
    unit_start = min((o for o, n in pairs if o != 0xffffffff), default=27)
    out = dict(code=code, pairs=pairs, units=units, nodes=[], countries=w[26:unit_start], trailer=w[unit_end:])
    if 0 not in units: return out
    u = units[0]
    out['nnodes'] = (u[0] << 16) | u[1]; out['nlinks'] = (u[2] << 16) | u[3]
    out['classes'] = [(u[4 + 2 * i], u[5 + 2 * i]) for i in range(11)]
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

def unit0_words(rec):
    """Rebuild unit 0 from parse_record's semantic node and entry fields."""
    out = [rec['nnodes'] >> 16, rec['nnodes'] & 0xffff,
           rec['nlinks'] >> 16, rec['nlinks'] & 0xffff]
    out += [v for pair in rec['classes'] for v in pair]
    for nd in rec['nodes']:
        out += [len(nd['entries']) << 12 | nd['xflags'] | nd['x'],
                nd['ncls'] << 12 | (nd['yflags'] & 1) << 11 | nd['y'], nd['nflags']]
        for e in nd['entries']:
            out += [e['other'], e['w4'], e['w5'], e['id'], e['cls'], e['a'], e['b']]
            out += e['extra']
    return out

def encode_record(rec):
    """Re-encode a parsed section-2 record, rebuilding unit 0 from its decoded fields."""
    units = dict(rec['units'])
    if 0 in units: units[0] = unit0_words(rec)
    live = [(o, n) for o, n in rec['pairs'] if o != 0xffffffff]
    end = max((o + n for o, n in live), default=27) + len(rec['trailer'])
    w = [0] * end
    w[0:2] = rec['code']
    for i, (o, n) in enumerate(rec['pairs']):
        w[6 + 4 * i:10 + 4 * i] = [o >> 16, o & 0xffff, n >> 16, n & 0xffff]
    w[26:26 + len(rec.get('countries', ()))] = rec.get('countries', ())
    for i, (o, n) in enumerate(rec['pairs']):
        if o != 0xffffffff:
            assert len(units[i]) == n
            w[o:o + n] = units[i]
    if rec['trailer']: w[-len(rec['trailer']):] = rec['trailer']
    return struct.pack('>%dH' % len(w), *w)

def parse_part_b(part_b):
    """Decode the raw child-level pairing part used by level-2, level-4 and level-5 records."""
    rec = parse_record(part_b)
    assert 1 in rec['units'], 'part B has no unit 1'
    u = rec['units'][1]
    out = dict(record=rec, count=u[0], marker=u[1], nodes=[])
    p = 2
    for _ in range(out['count']):
        count_flags, upper, parcel, child = u[p:p + 4]; p += 4
        count = count_flags >> 12
        entries = [tuple(u[p + 3 * i:p + 3 * i + 3]) for i in range(count)]
        p += 3 * count
        out['nodes'].append(dict(flags=count_flags & 0xfff, upper=upper,
                                 parcel=parcel, child=child, entries=entries))
    out['tail'] = u[p:]
    out['consumed'] = p + len(out['tail'])
    return out

def encode_part_b(pb):
    """Re-encode parse_part_b's semantic pairing blocks."""
    u = [pb['count'], pb['marker']]
    assert len(pb['nodes']) == pb['count']
    for nd in pb['nodes']:
        u += [len(nd['entries']) << 12 | nd['flags'], nd['upper'], nd['parcel'], nd['child']]
        for e in nd['entries']: u += e
    u += pb['tail']
    rec = dict(pb['record']); rec['units'] = dict(rec['units']); rec['units'][1] = u
    return encode_record(rec)
