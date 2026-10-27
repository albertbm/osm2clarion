#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Encode a section-2 (road) level-1 record and directory from a node/link graph.

Graph: nodes [(x, y)] in parcel units, links [(i, j, length_m)]. The per-entry words `a`
(travel time) and `b` (length) are KIWI scaled values (KIWI 1.22 10.10.2): bits 14-12 a
multiplier m, bits 11-0 the value, unit 4**m -- 0.1 s for time, 1 m for length.
"""
import math, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, fa_encode

CLASS = 0x0898
END_FLAG = 0x2000    # on the link-id word of the entry at the link's drawn last point
MAXVAL = 0xffd       # 0xfff means infinite and 0xffe unknown, so a real value stops below them

# Default speed per class when the OSM way has no maxspeed, the median of Iceland ways that do.
SPEED_KMH = {
    'motorway': 90, 'motorway_link': 50,
    'trunk': 90, 'trunk_link': 50,
    'primary': 70, 'primary_link': 50,
    'secondary': 50, 'secondary_link': 50,
    'tertiary': 50, 'tertiary_link': 50,
    'unclassified': 50,
    'residential': 30,
    'living_street': 15,
    'service': 30,
    'track': 30,
}
DEFAULT_SPEED_KMH = 50

def speed_of(hw, maxspeed=None, unit='km/h'):
    """km/h for one link: the way's own maxspeed (a bare number read in `unit`) when it
    parses, else the class default. Shares its parsing with fa_encode.speed_limit."""
    p = fa_encode.parse_speed(maxspeed, unit)
    if p is not None:
        v = round(fa_encode.to_kmh(*p))
        if 1 <= v <= 200: return v
    return SPEED_KMH.get(hw, DEFAULT_SPEED_KMH)

def scaled(q):
    """(m, value) with value <= MAXVAL and unit 4**m, m as small as it goes. q is in units."""
    m, v = 0, max(1, round(q))
    while v > MAXVAL:
        m += 1
        v = max(1, round(q / 4 ** m))
        assert m <= 7, 'quantity %r does not fit the three-bit multiplier' % q
    return m, v

def cost_words(length_m, kmh):
    """The entry's (travel time, length) word pair; bit 15 of the time word is left set (KIWI RESERVED, set on every road link)."""
    secs = length_m / (kmh / 3.6)
    mt, t = scaled(secs * 10)
    mb, b = scaled(length_m)
    return 0x8000 | (mt << 12) | t, (mb << 12) | b

def class_word(cls):
    """The link class word: class in the high byte, then 0x90 | class (0x80 | class above 9)."""
    return cls << 8 | (0x90 if cls <= 9 else 0x80) | cls

def pair_slot(cls):
    """Pair index at u4..u25 for a node class: 0-2 at u6, 3-7 at u8, 8 at u10, 9 at u12, 10 and up at u24."""
    return 1 if cls <= 2 else 2 if cls <= 7 else 3 if cls == 8 else 4 if cls == 9 else 10

def unit_head(ncls, degree):
    """Unit 0's 26 header words from each node's class and its count of real link ends (parcel-edge connections excluded)."""
    pairs = [[0, 0] for _ in range(11)]
    for c, d in zip(ncls, degree):
        pairs[pair_slot(c)][0] += 1; pairs[pair_slot(c)][1] += d
    nn, nl = len(ncls), sum(degree)
    assert all(n < 0x10000 and l < 0x10000 for n, l in pairs), 'a pair overflows its u16'
    return [nn >> 16, nn & 0xffff, nl >> 16, nl & 0xffff] + [w for p in pairs for w in p]

def record_head(level, y0, x0, row, col, units, country):
    """The 27-word record header: parcel code, two zero dwords, five unit pairs, ISO 3166 country code."""
    assert country, 'a section-2 record needs its ISO 3166 country code'
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]
    hdr = list(clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)) + [0, 0, 0, 0]
    out = struct.pack('>6H', *hdr)
    for off, ln in units + [(0xffffffff, 0)] * (5 - len(units)): out += struct.pack('>II', off, ln)
    return out + struct.pack('>H', country), hdr

def node_classes(nodes, links, link_cls):
    """A node's class is the lowest class among its links."""
    ncls = [None] * len(nodes)
    for (i, j, _), c in zip(links, link_cls):
        for n in (i, j):
            ncls[n] = c if ncls[n] is None else min(ncls[n], c)
    return ncls

def build_record(level, y0, x0, row, col, nodes, links, compress=True, speeds=None, country=None, ids=None):
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    for k, (i, j, length) in enumerate(links):
        lid = ids[k] if ids else k + 1
        a, bw = cost_words(length, speeds[k] if speeds else DEFAULT_SPEED_KMH)
        for end, other in ((i, j), (j, i)):
            adj[end].append((pos[other], lid | (END_FLAG if end == j else 0), a, bw))
    body = []
    for old in order:
        x, y = nodes[old]; ents = adj[old]
        assert ents, 'isolated node'
        assert len(ents) <= 15, 'node %r has %d entries, too many for the count nibble' % (nodes[old], len(ents))
        body += [(len(ents) << 12) | x, (CLASS & 0xf) << 12 | y, 0]
        for other, lid, a, bw in ents:
            body += [other, 0, 0, lid, CLASS, a, bw]
    unit = unit_head([CLASS & 0xf] * len(nodes), [len(adj[o]) for o in order]) + body
    out, _ = record_head(level, y0, x0, row, col, [(0x1b, len(unit))], country)
    assert len(out) == 54
    out += struct.pack('>%dH' % len(unit), *unit)
    if not compress: return out, out          # level-2 records ship raw
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(out) + c.flush()
    if len(comp) % 2: comp += b'\0'
    return out, comp

def oneway_bits(d):
    """The w5 bits for a link's (i end, j end): d = 1 one-way i to j, -1 j to i, 0 both ways.
    Traffic leaves the 0x4000 end and arrives at the 0x8000 end."""
    return (0x4000, 0x8000) if d > 0 else (0x8000, 0x4000) if d < 0 else (0, 0)

SIDE_B = {0x8000: 0x8000, 0xa000: 0x0080, 0xc000: 0xc000, 0xe000: 0x00c0}   # part-b side flags (top, right, bottom, left)

PROHIBITED = 0xff     # regulation flag (bit 7) + passage code 0x7f, "not allowed unconditionally"

def regulation_word(entry_index):
    """One regulation record: byte 0 entry-side link number (bits 7-4, 15 = all entries), exit
    nibble left clear, byte 1 the flag plus passage code."""
    assert 0 <= entry_index <= 15
    return (entry_index << 12) | PROHIBITED

ALL_ENTRIES = 15      # the from nibble that names every entry at the node

def oneway_regulations(adj, real):
    """{(node, entry number): [regulation words]} around one-way links: the arriving entry
    gets no-entry-from-anyone, every entry beside a leaving one gets barred from it."""
    out = {}
    for nd, ents in adj.items():
        leaving = [k for k in range(real[nd]) if ents[k][5] == 0x4000 and k <= 14]
        for k in range(real[nd]):
            if ents[k][5] == 0x8000:
                out[(nd, k)] = [regulation_word(ALL_ENTRIES)]
            elif leaving:
                out[(nd, k)] = [regulation_word(j) for j in leaving]
    return out

def regulation_order(words, k):
    """Entry k's regulation words sorted by from-entry, cyclically from k + 1, so a
    prohibition from the entry itself comes last."""
    return sorted(words, key=lambda w: ((w >> 12) - k - 1) % 16)

def build_record_edges(level, y0, x0, row, col, nodes, links, edges, compress=True, classes=None,
                       speeds=None, regs=None, country=None, dirs=None, ids=None, oneway_regs=True):
    """build_record with parcel-edge connections: edges = {node index: (side bits, twin index)}.
    Level 1 ships zlib; level 2 raw, as two parts, the second a per-node connection table."""
    default = 0x0898 if level == 1 else 0x0797
    cls_of = [class_word(fa_encode.layer_class(hw, level)[1]) for hw in classes] if classes else [default] * len(links)
    ncls = node_classes(nodes, links, [c & 0xf for c in cls_of])
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    slot = {}
    for k, (i, j, length) in enumerate(links):
        lid = ids[k] if ids else k + 1
        a, bw = cost_words(length, speeds[k] if speeds else DEFAULT_SPEED_KMH)
        low = cls_of[k] & 0xff
        ow = oneway_bits(dirs[k] if dirs else 0)
        for end, other in ((i, j), (j, i)):
            slot[(end, k)] = len(adj[end])
            adj[end].append((pos[other], lid | (END_FLAG if end == j else 0), ncls[other] << 8 | low, a, bw,
                             ow[0] if end == i else ow[1]))
    real = {i: len(v) for i, v in adj.items()}
    for i, (side, twin) in edges.items():
        adj[i].append((twin, 0, side | ncls[i] << 8 | (adj[i][0][2] & 0xff), 0x8000, 0, 0))
    extras = {}
    build_record_edges.regs_written = 0
    build_record_edges.regs_dropped = 0
    if oneway_regs and dirs:
        for (nd, k), words in oneway_regulations(adj, real).items():
            extras[(nd, k)] = words
    for nd, fl, tl in regs or []:
        fi, ti = slot.get((nd, fl)), slot.get((nd, tl))
        if fi is None or ti is None or fi > 14 or ti > 14:
            build_record_edges.regs_dropped += 1; continue
        w = regulation_word(fi)
        lst = extras.setdefault((nd, ti), [])
        if w in lst or regulation_word(ALL_ENTRIES) in lst: continue   # already prohibited
        if len(lst) >= 14:          # w5 bits 6-11 hold the count; 14 is as far as this was tested
            build_record_edges.regs_dropped += 1; continue
        lst.append(w); build_record_edges.regs_written += 1
    body = []
    for old in order:
        x, y = nodes[old]; ents = adj[old]
        if not ents: continue
        # the entry count shares its word with x, so 15 is the ceiling; above it x is corrupted
        assert len(ents) <= 15, 'node %r has %d entries, too many for the count nibble' % (nodes[old], len(ents))
        body += [(len(ents) << 12) | x, ncls[old] << 12 | y, 0]
        for k, (other, lid, cls, a, length, ow) in enumerate(ents):
            ex = regulation_order(extras.get((old, k), ()), k)
            body += [other, 0, ow | len(ex) << 6, lid, cls, a, length] + ex
    live = [old for old in order if adj[old]]
    nn = len(live)
    unit = unit_head([ncls[old] for old in live], [real[old] for old in live]) + body
    out, hdr = record_head(level, y0, x0, row, col, [(0x1b, len(unit))], country)
    out += struct.pack('>%dH' % len(unit), *unit)
    if level == 1:
        c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(out) + c.flush()
        if len(comp) % 2: comp += b'\0'
        return out, comp
    pb = [nn, 0xa100]   # part b: connection table
    for k, old in enumerate(o for o in order if adj[o]):
        pb += [0x2000, k, 0, k]
        for other, lid, cls, a, length, _ in adj[old]:
            pb += [other, SIDE_B.get(cls & 0xe000, 0) if cls & 0x8000 else 0, other]
    pb += [hdr[0], hdr[1], 0]
    partb, _ = record_head(level, y0, x0, row, col, [(0xffffffff, 0), (0x1b, len(pb))], country)
    partb += struct.pack('>%dH' % len(pb), *pb)
    return out, partb

def make_dir(level, y0, x0, recs):
    """Section-2 directory: kind 0x1046, one pair per column, 32 slots of parcel rows."""
    typ = {1: 0x90, 2: 0x81}[level]
    body = 256; npairs = body // 8
    slots = {}
    for row, cols in sorted(recs.items()):
        pairs = [b'\xff' * 8] * npairs; rlist = []
        for col, (plain, comp) in sorted(cols.items()):
            if level == 1:
                pairs[col] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
                rlist.append(dict(k=col, raw=comp, words=len(plain) // 2))
            else:
                pairs[col] = struct.pack('>II', len(plain) // 2, len(comp) // 2)
                rlist.append(dict(k=col, raw=plain + comp, words=len(comp) // 2))
        slots[row] = dict(body=b''.join(pairs), recs=rlist)
    n = {1: 1, 2: 4}[level]
    return dict(kind=0x1046, ab=(32, 32), y=y0, x=x0, typ=typ, body=body, nslots=32, slots=slots,
                empty_bodies={i: b'\xff' * body for i in range(32) if i not in slots}, box=(y0 + n, y0, x0, x0 + n))
