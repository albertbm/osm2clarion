#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The road-shape record (kind 0x00fa) in the even pair of a level-1 background directory:
header, a geometry unit of twelve layer pairs, then the shapes, one per drawn link chain."""
import collections, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg
import clarion_format

FMT = clarion_format.DEFAULT   # set by rd_build from the profile

ROAD_LABEL = 0x00ff   # type word of a road label entry; a place, water or POI name has another
STYLE = 0x101e        # the style word 79,182 of 128,000 shipped level-1 shapes carry: no limit
FERRY_STYLE = 0x001e  # measured on every class-10 shape at both levels; style_word's oneway
                      # bit would also clear bit 12, but a ferry link is two-way

def style_word(limit, oneway=False):
    """Level-1 style word for a posted limit, or the plain one for none. A one-way link
    clears the two-way bit, bit 12 on QY8 and bit 13 on QY7."""
    w = 0x1e if not limit or limit % 5 or not 5 <= limit <= 160 else 0x20 | (limit // 5 - 1)
    return w if oneway else w | FMT.twoway

def limit_of(style):
    """The posted limit a style word carries, or None."""
    return ((style & 0x1f) + 1) * 5 if style & 0x20 else None

def parse_speed(maxspeed, unit='km/h'):
    """(value, unit) for the first ';'-part of an OSM maxspeed tag ('50', '50.0', '30 mph'): an
    explicit 'mph' or 'km/h' suffix names its own unit, else it's posted in `unit`. None when it
    does not parse. Shared with rd_encode.speed_of, which wants the raw km/h instead of a signed,
    rounded limit."""
    if not maxspeed: return None
    parts = str(maxspeed).strip().lower().split(';')[0].split()
    try: v = float(parts[0])
    except (ValueError, IndexError): return None
    src = parts[1] if len(parts) > 1 and parts[1] in ('mph', 'km/h') else unit
    return v, src

def to_kmh(v, src):
    return v * 1.609344 if src == 'mph' else v

def speed_limit(maxspeed, unit='km/h'):
    """OSM maxspeed as a posted limit in `unit`, or None when it isn't a multiple of 5."""
    p = parse_speed(maxspeed, unit)
    if p is None: return None
    v, src = p
    if src != unit: v = v / 1.609344 if unit == 'mph' else v * 1.609344
    v = round(v)
    if src != unit: v = 5 * round(v / 5)
    return v if v % 5 == 0 and 5 <= v <= 160 else None

# OSM highway -> (level-1 layer, class), level-2 class, level-3 class; class follows road standard, not route prefix.
# A road carried at level 2 keeps its level-2 class at level 1: on factory B30R070R every one of the 36,326
# level-2 links steps onto a level-1 link of the same class, and classes 8 and 9 are level-1-only roads.
CLASSES = {
    'motorway':       ((7, 1), 1, 3),
    'motorway_link':  ((7, 1), 1, 3),
    'trunk':          ((6, 3), 3, 4),
    'trunk_link':     ((6, 3), 3, 4),
    'primary':        ((6, 4), 4, 5),
    'primary_link':   ((6, 4), 4, 5),
    'secondary':      ((6, 5), 5, 6),
    'secondary_link': ((6, 5), 5, 6),
    'tertiary':       ((6, 7), 7, 7),
    'tertiary_link':  ((6, 7), 7, 7),
    'unclassified':   ((5, 8), 7, 7),
    'residential':    ((5, 8), 7, 7),
    'living_street':  ((5, 8), 7, 7),
    'service':        ((4, 9), 7, 7),
    'track':          ((4, 9), 7, 7),
    'ferry':          ((0, 10), 10, 10),
}
DEFAULT = ((5, 8), 7, 7)

def layer_class(hw, level, layer=None):
    """(layer, class) for one OSM highway value at level 1 or 2."""
    (l1, c1), c2, _ = CLASSES.get(hw, DEFAULT)
    if level == 1:
        lay, cls = l1, c1
    else:
        cls = c2
        lay = 0 if cls == 10 else (7 if cls in (1, 2) else 6)   # a ferry stays in layer 0 at L2 too
    return (layer if layer is not None else lay), cls

def restyled(styles):
    """Per link, whether its header carries a style word: the first always, a later one only where it changes."""
    return [n == 0 or styles[n] != styles[n - 1] for n in range(len(styles))]

def shape_words(chain, nodes, name_off=0xffff, level=1, cls=None, styles=None, dirs=None, gs=None, ring=False, upper=()):
    """Section-1 shape words for one link chain: start point, then each link's header and
    points, with x flags 0x4000/0x2000/0x1000/0x0800 marking link starts, style changes and
    ends (docs/GEOMETRY.md), and the one-way sense OR'd into bits 15-14 of y.

    gs (level 1 only) maps a node occurrence to its guidance word G: occurrence n is the
    start node of link n, occurrence len(chain) the chain's last node. The occurrence's y
    gets bit 0x1000 and G follows its key word.

    upper: the upper-level links the chain is part of, highest level first, one word each
    (UPPER_LEVEL[level] | link id). The unit finds an upper-level route link's level-1 links
    by them (Navi.exe 0x34bf3c, 0x34d3e4)."""
    if cls is None: cls = 8 if level == 1 else 7
    if styles is None or level != 1: styles = [style_word(None)] * len(chain)
    if dirs is None: dirs = [0] * len(chain)
    if gs is None or level != 1: gs = {}
    fresh = restyled(styles)
    def dirbits(n):
        d = dirs[n]
        return 0x4000 if d > 0 else 0x8000 if d < 0 else 0
    # word 3 bits 14-12 = 7 on factory ring strings (Koln 89%, UK 95%, ~0% elsewhere): the
    # 3-bit code section 2 carries in w4. Word 4: word offset of the name entry, 0xffff = none
    code = 0x7000 if ring else 0
    if level == 1: out = [cls << 12, code | FMT.shape_w3, name_off, FMT.shape_w5, *upper, 0x0000]
    else: out = [cls << 12, code, name_off, 0x0000, *upper, 0x0000]
    sx, sy = nodes[chain[0][1]]
    out += [0x0800 | sx, 0x2800 | sy | dirbits(0) | (0x1000 if 0 in gs else 0)]
    npts = 1
    for n, (lid, a, b, inner) in enumerate(chain):
        # key placeholder: build_record overwrites it once it knows every node's other
        # occurrences in the record; 0xffff here matches a node with no twin at all
        if level == 1: out += ([styles[n]] if fresh[n] else []) + [0xffff] + ([gs[n]] if n in gs else []) + [lid]
        # a ferry link is two-way, but style_word's oneway test would still clear its bit 12
        elif n == 0: out += [FMT.ferry_style if cls == 10 else style_word(None, oneway=dirs[n] != 0), lid]
        else: out += [lid]
        pts = list(inner) + [nodes[b]]
        for k, (x, y) in enumerate(pts):
            first = k == 0; link_end = k == len(pts) - 1; chain_end = n == len(chain) - 1
            if level == 1:
                xf = ((0x4000 if first else 0) | (0x2000 if first and fresh[n] else 0)
                      | (0 if link_end and not chain_end else 0x1000)
                      | (0 if link_end else 0x0800))
                if link_end and not chain_end: y |= 0x2800 if fresh[n + 1] else 0x0800
                if link_end and n + 1 in gs: y |= 0x1000
            else:
                xf = ((0x4000 if n == 0 else 0x2000) if first else 0) \
                     | (0 if link_end and not chain_end else 0x1000) | (0 if link_end else 0x0800)
                if link_end and not chain_end: y |= 0x0800
            out += [xf | x, y | dirbits(n)]; npts += 1
    # tail word: the chain's last node's key (build_record fills it in); 0xffff default = no twin
    if level == 1: out += [0xffff] + ([gs[len(chain)]] if len(chain) in gs else [])
    body = [0x8000 | npts] + out
    return [len(upper) << 13 | len(body) + 1] + body

UPPER_LEVEL = {2: 0x2000, 4: 0x6000, 5: 0x8000}   # level code, top three bits of an upper-link word
# Bit 14 of a shape's point-count word: the road is in its country's traffic (TMC) location
# table. The traffic module lists only such shapes (Navi.exe 0x4de924). Never written here:
# factory shapes in a country with no table (Belarus, Albania) have it clear too.
TRAFFIC = 0x4000

def header_words(fresh, has_g):
    """Words of one link header: [style] key [G] lid."""
    return 2 + (1 if fresh else 0) + (1 if has_g else 0)

def point_words(chain, level=1, styles=None, gs=None, head=0):
    """[(word offset, link id, role)] for every point shape_words writes; role is start, end or vertex.
    head: upper-link words in the shape's header."""
    fresh = restyled(styles) if styles and level == 1 else [n == 0 and level == 1 for n in range(len(chain))]
    if gs is None or level != 1: gs = {}
    out = [(7 + head, chain[0][0], 'start')]
    q = 9 + head
    for n, (lid, a, b, inner) in enumerate(chain):
        q += header_words(fresh[n], n in gs)
        for k in range(len(inner) + 1):
            out.append((q, lid, 'end' if k == len(inner) else 'vertex')); q += 2
    return out

def key_slots(chain, styles=None, gs=None, head=0):
    """Level-1 only. Per node occurrence in one shape chain: (key slot offset, x-word
    offset, node index), both relative to the shape's own word 0 (its length word). The
    key slot is where a ring pointer gets WRITTEN (one per link header, the link's start
    node, plus one for the chain's last node, landing on the shape's trailer word); the
    x-word offset is what a pointer TO this occurrence must read (the card points at a
    node's coordinate pair, not at another node's key slot: in one factory record key 02bc
    -> w722 lands on w722's x word). len(chain)+1 entries, in the
    order the firmware would read them off the card. A G word, when gs has one for the
    occurrence, sits at key slot + 1."""
    fresh = restyled(styles) if styles else [n == 0 for n in range(len(chain))]
    if gs is None: gs = {}
    q = 9 + head; px = 7 + head; slots = []
    for n, (lid, a, b, inner) in enumerate(chain):
        slots.append((q + (1 if fresh[n] else 0), px, a))
        q += header_words(fresh[n], n in gs) + 2 * (len(inner) + 1)
        px = q - 2   # this link's end point (the next occurrence's x word)
    slots.append((q, px, chain[-1][2]))
    return slots

def identical_node_keys(chains, styles, at, order, gs=None, heads=None):
    """{absolute word offset in `unit`: key value} for every node-record key slot across a
    whole level-1 0x00fa record (measured on factory parcels a40c d03e, a73c d34c, a8ef
    cd6c: a ring in every case, 222,878 of 222,878 three-member groups and 27,827 of 27,827
    larger ones checked over two factory tiles). Each occurrence of a node points at the
    x word of the occurrence just before it in record word order; the first occurrence
    wraps to the last. 0xffff when a node has no other occurrence in this record (true for
    every parcel-edge node: the factory links those across the tile boundary into the
    neighbouring record, which we don't do here). at[i] is each chain's shape offset within
    `unit`; order is the chain indices in the sequence build_record actually laid them out
    in (ascending by layer, not by chain index, so it is not the same as sorted(at))."""
    occ = []   # (slot abs offset, x-word abs offset, node index), true record order
    for i in order:
        for slot, xoff, nid in key_slots(chains[i], styles[i] if styles[i] else None, gs.get(i) if gs else None,
                                         heads[i] if heads else 0):
            occ.append((at[i] + slot, at[i] + xoff, nid))
    by_node = collections.defaultdict(list)
    for k, (_, _, nid) in enumerate(occ):
        by_node[nid].append(k)
    out = {}
    for nid, ks in by_node.items():
        if len(ks) == 1:
            out[occ[ks[0]][0]] = 0xffff
        else:
            for p, k in enumerate(ks):
                out[occ[k][0]] = occ[ks[p - 1]][1]   # previous occurrence's x word; p=0 wraps to the last
    return out

def item_words(shape, level=1, name_off=0xffff, country=None, area=None):
    """Names sub-table item for a shape; word 5 is the ISO 3166 country code. area is the
    three area codes a QY7 item ends in at both levels, or None for the QY8 forms."""
    assert country, 'item_words needs the country code'
    npts = shape[1] & 0x3fff   # word 1 is 0x8000 | points
    if area is not None: return [0x8000, 9, 0x8000 | (2 * max(0, npts - 2)), 6, 0x0bc0, country, *area]
    if level == 1: return [0x8000, 9, 0x8000 | (2 * max(0, npts - 2)), 6, 0x0bc0, country, 0xe, 8, 0xd]
    named = name_off != 0xffff
    return [0x8000, 9, 0x8000 | (2 * max(0, npts - 2)), 6, 0x0bc0, country, 1, 1 if named else 0, name_off if named else 0]

def tail_items(n):
    """Level-1 tail run items for n = shapes + links: chunks of at most 127, each (count << 8) | 1."""
    items = []
    while n > 0:
        take = min(127, n)
        items.append((take << 8) | 1)
        n -= take
    return items

def tail_count(items):
    """shapes + links from a tail's run items; only type-1 items (bit 15 clear) are counted."""
    return sum(x >> 8 for x in items if not x & 0x8000 and x & 0xff == 1)

def tail_of(plain):
    """A built record's tail: (item count, items). Level 1 only; (0, []) without a tail."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    off, cnt = w[17], w[18]
    if off == 0xffff: return 0, []
    tail = w[2 * off: 2 * off + 2 * cnt]
    return tail[1], list(tail[8:8 + tail[1]])

def junctions(chains):
    """Nodes of a parcel that three or more links meet at."""
    deg = collections.Counter()
    for c in chains:
        for lid, a, b, inner in c: deg[a] += 1; deg[b] += 1
    return sum(1 for n in deg.values() if n >= 3)

def build_record(level, y0, x0, row, col, nodes, chains, layer=None, name_off=0xffff, name_offs=None, hws=None,
                 styles=None, country=None, dirs=None, gs=None, rings=None, area=None, uppers=None):
    """A level-1 or level-2 road-shape record and its compressed form, shapes grouped by layer.
    uppers: each chain's upper-link words (shape_words).

    gs, level 1 only: {chain index: {node occurrence: G}} as shape_words takes it. G is the
    link-word index of the occurrence's section-4 sub-record, which depends on this record's
    layout; the layout does not depend on G's value, so build twice: once with placeholder
    values to read the node word offsets off last_index, then with the real ones."""
    assert country, 'build_record needs the country code'
    if hws is None: hws = [None] * len(chains)
    lc = [layer_class(hw, level, layer) for hw in hws]
    if styles is None: styles = [None] * len(chains)
    if dirs is None: dirs = [None] * len(chains)
    if gs is None or level != 1: gs = {}
    if uppers is None: uppers = [()] * len(chains)
    heads = [len(u) for u in uppers]
    shapes = [shape_words(c, nodes, name_offs[i] if name_offs else name_off, level, lc[i][1], styles[i], dirs[i], gs.get(i),
                          ring=bool(rings and rings[i]), upper=uppers[i])
              for i, c in enumerate(chains)]
    if level == 1: unit = [junctions(chains)] + [0xffff, 0x0000] * 12 + [0x0000]; first = 1
    else: unit = [0xffff, 0x0000] * 11; first = 0   # no count word
    order = []; at = {}
    for lay in sorted({l for l, _ in lc}):
        grp = [i for i, (l, _) in enumerate(lc) if l == lay]
        unit[first + 2 * lay] = len(unit) // 2       # offset in dwords from the unit start
        unit[first + 1 + 2 * lay] = len(grp)
        for i in grp: at[i] = len(unit); unit += shapes[i]
        if len(unit) % 2: unit.append(0)   # a group of odd word count is padded, as shipped units are
        order += grp
    if len(unit) % 2: unit.append(0)
    if level == 1:   # identical-node key chain, computed with the whole record in view
        for off, key in identical_node_keys(chains, styles, at, order, gs, heads).items():
            unit[off] = key
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]
    code = clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)
    nlinks = sum(len(c) for c in chains)
    items = tail_items(len(shapes) + nlinks)
    tail = [0x0004, len(items), 0xffff, 0x0000, 0xffff, 0x0000, 0xffff, 0x0000] + items
    if len(tail) % 2: tail.append(0)
    if not FMT.tail_runs: tail = []   # nothing here is restricted, and QY7 then has no tail
    if level == 1:
        w = [code[0], code[1], 0x00fa, 0x347a, 0x2aef, 0, 0, 11, len(unit) // 2, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0,
             11 + len(unit) // 2 if tail else 0xffff, len(tail) // 2, FMT.shape_w19, country, 0]
        plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit) + struct.pack('>%dH' % len(tail), *tail)
    else:
        w = [code[0], code[1], 0x00fa, 0xffff, 0xffff, 0, 0, 11, len(unit) // 2, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0,
             0xffff, 0, 0, country, 0]
        plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit)
    comp = FMT.deflate(plain)
    build_record.last_items = [item_words(shapes[i], level, (name_offs[i] if name_offs else name_off), country, area)
                               for i in order]
    # where each chain landed, in record words: the address files point at these
    build_record.last_index = dict(code=code, unit=2 * w[7], shapes=[
        dict(chain=i, at=2 * w[7] + at[i], layer=lc[i][0], cls=lc[i][1], length=len(shapes[i]),
             points=[(2 * w[7] + at[i] + o, lid, role) for o, lid, role in point_words(chains[i], level, styles[i], gs.get(i), heads[i])])
        for i in sorted(at)])
    return plain, comp

def occurrence_words(index):
    """{chain index: [record word offset of each node occurrence's x word]} from a last_index."""
    return {S['chain']: [o for o, _, role in S['points'] if role in ('start', 'end')] for S in index['shapes']}

STRING_HDR = {0: 7, 2: 8, 4: 9, 6: 10}   # words from the length word to the first point, by the type in word 0's top nibble

def link_strings(plain):
    """Every link string of a level-1 0x00fa record with its node records, in the factory's
    grammar: [dict(at, typ, layer, pts, tail)], pts =
    [(x-word offset, x, y, x flags, y flags, node)], node = dict(style, key, G, seq) on the
    first point and on every point whose y has bit 0x0800, and dict(key, G) for the last
    point, whose key is the tail word and whose G follows it when its y has bit 0x1000.
    Offsets are record words; the key space the card uses is offset - 22."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    g = 2 * w[7]
    out = []
    for li in range(12):
        off, cnt = w[g + 1 + 2 * li], w[g + 2 + 2 * li]
        if off == 0xffff: continue
        i = g + 2 * off
        for _ in range(cnt):
            typ, ln = w[i] >> 12, w[i] & 0xfff
            if typ not in STRING_HDR: raise ValueError('link string type %d at word %d' % (typ, i))
            r = w[i:i + ln]; npts = r[1] & 0xfff; j = STRING_HDR[typ]; pts = []
            for k in range(npts):
                x, y = r[j], r[j + 1]; xo = i + j; j += 2; node = None
                if k == 0 or y & 0x0800:
                    style = None
                    if y & 0x2000: style = r[j]; j += 1
                    key = r[j]; j += 1; G = None
                    if y & 0x1000: G = r[j]; j += 1
                    seq = r[j]; j += 1
                    node = dict(style=style, key=key, G=G, seq=seq)
                pts.append((xo, x & 0x7ff, y & 0x7ff, x >> 11, y >> 11, node))
            tail = tuple(r[j:])
            if tail and pts[-1][5] is None:
                xo, x, y, xf, yf, _ = pts[-1]
                pts[-1] = (xo, x, y, xf, yf, dict(style=None, key=tail[0], G=tail[1] if yf & 2 and len(tail) > 1 else None, seq=None))
            out.append(dict(at=i, typ=typ, layer=li, hdr=tuple(r[2:STRING_HDR[typ]]), pts=pts, tail=tail))
            i += ln
    return out

def parse_record(plain, level=1):
    """Shapes of a 0x00fa record, parsed with the factory's own grammar.
    Returns (code, unit start, [shape dicts])."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    assert w[2] == 0x00fa, hex(w[2])
    g = 2 * w[7]
    shapes = []
    layers, t0 = (12, 1) if level == 1 else (11, 0)    # level 1 has a count word before the pairs
    for li in range(layers):
        off, cnt = w[g + t0 + 2 * li], w[g + t0 + 1 + 2 * li]
        if off == 0xffff: continue
        p = g + 2 * off
        for _ in range(cnt):
            if p >= len(w): break
            L = w[p] & 0x1fff
            h = 7 + (1 if w[p] & 0x2000 else 0) + (2 if w[p] & 0x4000 else 0)
            end = min(p + L - (1 if level == 1 else 0), len(w) - 1 if level == 1 else len(w))
            pts = {p + h: ('start', None)}
            q = p + h + 2; prev_y = w[p + h + 1]; links = []; styles = []; ends = []; style = None
            cur = (w[p + h] & 0x7ff, w[p + h + 1] & 0x7ff)
            while q + 2 < end:
                if prev_y & 0x2000: style = w[q]; q += 1
                styles.append(style)
                if level == 1: q += 1                   # val
                if prev_y & 0x1000: q += 1
                lid = w[q]; q += 1; links.append(lid)
                if pts[p + h][1] is None: pts[p + h] = ('start', lid)
                first = cur
                while q + 1 <= end and q + 1 < len(w) and len(pts) < (w[p + 1] & 0x3fff):
                    x, y = w[q], w[q + 1]; q += 2
                    last = not x & 0x1000 or q >= end or len(pts) + 1 == (w[p + 1] & 0x3fff)
                    pts[q - 2] = ('end' if last else 'vertex', lid)
                    prev_y = y; cur = (x & 0x7ff, y & 0x7ff)
                    if not x & 0x1000: break
                ends.append((first, cur))
            if level == 1 and prev_y & 0x1000: end -= 1   # the last node carries a G word after its tail key
            shapes.append(dict(at=p, length=L, layer=li, cls=w[p + 2] >> 12, name=w[p + 4], points=pts, links=links,
                               styles=styles, ends=ends, ok=q == end and len(pts) == (w[p + 1] & 0xff)))
            p += L
    return (w[0], w[1]), g, shapes

def names_section(labels, items, base=(b'', 0)):
    """Names section: road-label entries plus one sub-table item per shape. Returns (bytes, {label: word offset}).
    base: (label bytes, count) already in the record, kept in front so their offsets still hold.
    items None leaves the sub-table out, as a QY7 level-5 record does."""
    import struct as _s
    body = base[0]; offs = {}
    for name in labels:
        text = FMT.text(name)
        if len(text) % 2: text += b'\0'
        offs[name] = (24 + len(body)) // 2
        body += _s.pack('>HHHHH', ROAD_LABEL, FMT.label_flags, 0, 0, len(text)) + text
    while len(body) % 4: body += b'\0'
    if items is None:
        return _s.pack('>HH', 6, base[1] + len(labels)) + b'\xff\xff\x00\x00' * 5 + body, offs
    sub = _s.pack('>HH', 8, len(items)) + b'\xff\xff\x00\x00' * 3 + b''.join(_s.pack('>9H', *it) for it in items)
    while len(sub) % 4: sub += b'\0'
    hdr = _s.pack('>HH', 6, base[1] + len(labels)) + b'\xff\xff\x00\x00' + _s.pack('>HH', (24 + len(body)) // 4, len(items)) + b'\xff\xff\x00\x00' * 3
    return hdr + body + sub, offs

def chains_from_links(links, way_of, chain=False):
    """One shape per link, or (chain=True) consecutive links of one way merged into a chain."""
    chains = []
    for lid, (i, j, _) in enumerate(links, start=1):
        if chain and chains and way_of.get(lid) == way_of.get(lid - 1) and chains[-1][-1][2] == i:
            chains[-1].append((lid, i, j, []))
        else:
            chains.append([(lid, i, j, [])])
    return chains
