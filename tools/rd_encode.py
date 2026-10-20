#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Encode a section-2 (road) level-1 record and directory from a node/link graph.

Graph: nodes [(x, y)] in parcel units, links [(i, j, length_m)]. Follows the shipped layout
read by clarion_rd: nodes sorted by x, entries ordered as given, no parcel-edge connections.

The per-entry words `a` (travel time) and `b` (length) are KIWI scaled values: bits 14-12
a multiplication constant m, bits 11-0 the value, unit 4**m -- 0.1 s for the time, 1 m for
the length. See KIWI 1.22 10.10.2 (17)(18) and 7.2.2.1.1 (9-4-1). Decoding the 2014 card
that way gives sane speeds (B58R060R 1,989,501 level-1 links, median 29.8 km/h; B60R060R
42,876 links, median 49.9 km/h), and the multiplier is live there: `a >> 12` takes 8 to 11,
which is bit 15 set with m = 0..3.
"""
import math, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg, fa_encode

CLASS = 0x0898
MAXVAL = 0xffd       # 0xfff means infinite and 0xffe unknown, so a real value stops below them

# Travel speed in km/h when the OSM way carries no `maxspeed`, which is 41,000 of the 62,439
# Iceland ways. Each figure is the median of the ways of that class that DO carry one, so the
# default is what the same kind of road is tagged in the same country. Measured 6 Oct 2026
# over build/roads_iceland.json.
SPEED_KMH = {
    'motorway': 90, 'motorway_link': 50,       # no motorway in Iceland; the legal limit
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

def speed_of(hw, maxspeed=None):
    """km/h for one link: the way's own `maxspeed` when it parses, else the class default.
    Every Iceland value is a bare number, but `mph` is handled so a future extract of a
    country that uses it does not read 50 mph as 50 km/h."""
    if maxspeed:
        parts = str(maxspeed).strip().lower().split()
        try:
            v = int(parts[0])
            if len(parts) > 1 and parts[1] == 'mph': v = round(v * 1.609344)
            if 1 <= v <= 200: return v
        except (ValueError, IndexError):
            pass
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
    """The entry's (travel time, length) word pair. Bit 15 of the time word is left set, as
    this file has always written it: KIWI calls it RESERVED, but the 2014 card sets it on
    every level-1 road link and clears it on every ferry link."""
    secs = length_m / (kmh / 3.6)
    mt, t = scaled(secs * 10)                      # the time unit is 0.1 s
    mb, b = scaled(length_m)                       # the length unit is 1 m
    return 0x8000 | (mt << 12) | t, (mb << 12) | b

def class_word(cls):
    """The link class word: class in the high byte, then 0x90 | class. Measured over the 2014
    card's south-west Norway tile, where the common words are 0x898 (class 8, 102,045 links),
    0x595, 0x999, 0x494, 0x393, 0x797. The two constants this file used are that rule at
    classes 8 and 7. Classes above 9 ship as 0x80 | class instead (0xa8a, 0xc8c)."""
    return cls << 8 | (0x90 if cls <= 9 else 0x80) | cls

def pair_slot(cls):
    """Index of a node class in the unit's eleven (nodes, link ends) pairs at u4..u25: classes
    0-2 at u6, 3-7 at u8, 8 at u10, 9 at u12, 10 and up at u24 (rd_check.py has the evidence)."""
    return 1 if cls <= 2 else 2 if cls <= 7 else 3 if cls == 8 else 4 if cls == 9 else 10

def unit_head(ncls, degree):
    """Unit 0's 26 header words from each node's class and its count of real link ends.
    Parcel-edge connections are not link ends; the factory never counts them."""
    pairs = [[0, 0] for _ in range(11)]
    for c, d in zip(ncls, degree):
        pairs[pair_slot(c)][0] += 1; pairs[pair_slot(c)][1] += d
    nn, nl = len(ncls), sum(degree)
    assert all(n < 0x10000 and l < 0x10000 for n, l in pairs), 'a pair overflows its u16'
    return [nn >> 16, nn & 0xffff, nl >> 16, nl & 0xffff] + [w for p in pairs for w in p]

def record_head(level, y0, x0, row, col, units, country):
    """The 27-word record header: parcel code, two zero dwords, five unit pairs, then the ISO
    3166 numeric country code where unit 0 starts at word 27. Shipped records carry their own
    country here (380 Italy, 578 Norway); this file once wrote 0x017c and 0x00fa, which are
    Italy and France, read off the Pantelleria and Paris tiles it was first copied from."""
    assert country, 'a section-2 record needs its ISO 3166 country code'
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]   # parcels per mesh
    hdr = list(clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)) + [0, 0, 0, 0]
    out = struct.pack('>6H', *hdr)
    for off, ln in units + [(0xffffffff, 0)] * (5 - len(units)): out += struct.pack('>II', off, ln)
    return out + struct.pack('>H', country), hdr

def node_classes(nodes, links, link_cls):
    """A node's class is the lowest class among its links: the top nibble of its y word, and
    the high byte of every class word that points at it."""
    ncls = [None] * len(nodes)
    for (i, j, _), c in zip(links, link_cls):
        for n in (i, j):
            ncls[n] = c if ncls[n] is None else min(ncls[n], c)
    return ncls

def build_record(level, y0, x0, row, col, nodes, links, compress=True, speeds=None, country=None):
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    for lid, (i, j, length) in enumerate(links, start=1):
        yi, yj = nodes[i][1], nodes[j][1]
        north = i if (yi, nodes[i][0]) > (yj, nodes[j][0]) else j
        a, bw = cost_words(length, speeds[lid - 1] if speeds else DEFAULT_SPEED_KMH)
        for end, other in ((i, j), (j, i)):
            adj[end].append((pos[other], lid | (0x2000 if end == north else 0), a, bw))
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
    """The w5 bits for a link's (i end, j end): d = 1 one-way from i to j, -1 from j to i, 0
    both ways. Traffic leaves the 0x4000 end and arrives at the 0x8000 end. Measured 20 Oct
    2026 on factory level-1 and level-2 links: the two bits always sit at opposite ends, and
    on divided roads the twin carriageway lies to the left of that direction in France,
    Belgium, the Netherlands and Norway and to the right in England (REPORT-oneway in
    leaf-is-map). Parcel-edge entries never carry them."""
    return (0x4000, 0x8000) if d > 0 else (0x8000, 0x4000) if d < 0 else (0, 0)

SIDE_B = {0x8000: 0x8000, 0xa000: 0x0080, 0xc000: 0xc000, 0xe000: 0x00c0}   # part-b side flags (top, right, bottom, left)

PROHIBITED = 0xff     # between-links regulation flag (bit 7) + passage code 0x7f, "not allowed
                      # unconditionally" (KIWI 1.22 10.7.1.2 (4)(4-1))

def regulation_word(entry_index):
    """One regulation record, as the card writes them: byte 0 is the entry-side link record
    number in bits 7-4 and the exit-side one in bits 3-0, byte 1 the flag plus passage code.
    The exit nibble is 0 in all 2,346 records of the factory tile B60R060R and in the bulk of
    B58R060R's 279,072, so Clarion hangs the record off the entry for the exit link and leaves
    that nibble clear. 15 in the entry nibble is the all-entry-links wildcard."""
    assert 0 <= entry_index <= 15
    return (entry_index << 12) | PROHIBITED

def build_record_edges(level, y0, x0, row, col, nodes, links, edges, compress=True, classes=None,
                       speeds=None, regs=None, country=None, dirs=None):
    """build_record with parcel-edge connections: edges = {node index: (side bits, twin index)}.
    Level 1: zlib. Level 2: raw two parts, the second a per-node connection table. classes: per-link OSM highway value; each link then gets its
    own class word instead of the level's default. speeds: per-link km/h for the travel time.
    regs: [(node index, from link index, to link index)] turn prohibitions, written as
    regulation records on the entry for the to-link. Routing reads this, the renderer does not
    -- rewriting all 160,876 level-1 link words in a tile changed nothing on screen.

    Class words are written as the factory's are: the link's class in the low byte, the far
    node's class in the high byte, and on an edge entry the node's own class, since its twin
    across the edge carries the same roads. country: ISO 3166 numeric, word 26 of both parts.
    dirs: per link 1 one-way from i to j, -1 from j to i, 0 both ways (oneway_bits)."""
    default = 0x0898 if level == 1 else 0x0797
    cls_of = [class_word(fa_encode.layer_class(hw, level)[1]) for hw in classes] if classes else [default] * len(links)
    ncls = node_classes(nodes, links, [c & 0xf for c in cls_of])
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i][0], nodes[i][1]))
    pos = {old: new for new, old in enumerate(order)}
    adj = {i: [] for i in range(len(nodes))}
    slot = {}                       # (node index, link index) -> that link's entry number at the node
    for lid, (i, j, length) in enumerate(links, start=1):
        yi, yj = nodes[i][1], nodes[j][1]
        north = i if (yi, nodes[i][0]) > (yj, nodes[j][0]) else j
        a, bw = cost_words(length, speeds[lid - 1] if speeds else DEFAULT_SPEED_KMH)
        low = cls_of[lid - 1] & 0xff
        ow = oneway_bits(dirs[lid - 1] if dirs else 0)
        for end, other in ((i, j), (j, i)):
            slot[(end, lid - 1)] = len(adj[end])
            adj[end].append((pos[other], lid | (0x2000 if end == north else 0), ncls[other] << 8 | low, a, bw,
                             ow[0] if end == i else ow[1]))
    real = {i: len(v) for i, v in adj.items()}
    for i, (side, twin) in edges.items():
        # the edge entry repeats the class word of the link that reaches the edge
        adj[i].append((twin, 0, side | ncls[i] << 8 | (adj[i][0][2] & 0xff), 0x8000, 0, 0))
    extras = {}                     # (node index, entry number) -> [regulation words]
    build_record_edges.regs_written = 0
    build_record_edges.regs_dropped = 0
    for nd, fl, tl in regs or []:
        fi, ti = slot.get((nd, fl)), slot.get((nd, tl))
        if fi is None or ti is None or fi > 14 or ti > 14:
            build_record_edges.regs_dropped += 1; continue
        w = regulation_word(fi)
        lst = extras.setdefault((nd, ti), [])
        if w in lst: continue       # the same prohibition twice is one record
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
            ex = extras.get((old, k), ())
            body += [other, 0, ow | len(ex) << 6, lid, cls, a, length] + list(ex)
    live = [old for old in order if adj[old]]
    nn = len(live)
    unit = unit_head([ncls[old] for old in live], [real[old] for old in live]) + body
    out, hdr = record_head(level, y0, x0, row, col, [(0x1b, len(unit))], country)
    out += struct.pack('>%dH' % len(unit), *unit)
    if level == 1:
        c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(out) + c.flush()
        if len(comp) % 2: comp += b'\0'
        return out, comp
    # part b: connection table. Header nodes, 0xa100; per node 2000 k 0 k then per entry (other, side, other)
    pb = [nn, 0xa100]
    for k, old in enumerate(o for o in order if adj[o]):
        pb += [0x2000, k, 0, k]
        for other, lid, cls, a, length, _ in adj[old]:
            pb += [other, SIDE_B.get(cls & 0xe000, 0) if cls & 0x8000 else 0, other]
    pb += [hdr[0], hdr[1], 0]
    partb, _ = record_head(level, y0, x0, row, col, [(0xffffffff, 0), (0x1b, len(pb))], country)
    partb += struct.pack('>%dH' % len(pb), *pb)
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
