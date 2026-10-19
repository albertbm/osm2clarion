#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The road-shape record (kind 0x00fa) that sits in the even pair of a level-1 background
directory. Header: parcel code, 0x00fa, 347a 2aef, 0, 0, (w7, w8) = geometry unit at dword 11,
(w17, w18) = a 5-dword tail table, w19 0x8104, w20 the ISO country code. Geometry unit: u16 count, twelve
(u16 offset in dwords from the unit start, u16 shape count) layer pairs, a pad word, then the
shapes. Roads sit in the layer their class belongs to (layer 7 for classes 1-2, 6 for 3-6,
5 for 8, 4 for 9, 0 for ferries), and the class is the top nibble of shape word 2. A shape:
u16 length in words (itself included), 0x8000 | point count, class << 12, 0x0004, 0x000c,
0x8004, 0, then for each link of the chain (val, link id) and its points as (x | flags,
y | flags), the chain's start point first.
"""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg

STYLE = 0x101e        # the style word 79,182 of 128,000 shipped level-1 shapes carry: no limit

def style_word(limit):
    """Level-1 style word for a posted limit in the country's unit, or STYLE for none.
    Bit 5 marks a limit and the low five bits n give (n + 1) * 5, so 5..160 in steps of 5.
    Bits 6 and 12 are not understood; 0x1000 is kept as most factory words carry it."""
    if not limit or limit % 5 or not 5 <= limit <= 160: return STYLE
    return 0x1020 | (limit // 5 - 1)

def limit_of(style):
    """The posted limit a style word carries, or None."""
    return ((style & 0x1f) + 1) * 5 if style & 0x20 else None

def speed_limit(maxspeed, unit='km/h'):
    """OSM maxspeed as a posted limit in unit ('km/h' or 'mph'), or None. A bare number is
    km/h and '30 mph' is mph, as OSM tags them; a value in the other unit is converted and
    kept only if it lands on a multiple of 5. Zone and keyword values ('IS:urban', 'none',
    'walk', 'signals') give None, since no sign number is known for them."""
    if not maxspeed: return None
    parts = str(maxspeed).strip().lower().split(';')[0].split()
    try: v = float(parts[0])
    except (ValueError, IndexError): return None
    src = 'mph' if len(parts) > 1 and parts[1] == 'mph' else 'km/h'
    if src != unit: v = v / 1.609344 if unit == 'mph' else v * 1.609344
    v = round(v)
    if src != unit: v = 5 * round(v / 5)
    return v if v % 5 == 0 and 5 <= v <= 160 else None

# OSM highway -> (level-1 layer, level-1 class), level-2 class, level-3 class. Read off the
# 2014 card: class follows road standard, not route prefix, so Iceland's Route 1 and
# Reykjanesbraut are trunk and draw as through-roads.
CLASSES = {
    'motorway':       ((7, 1), 1, 3),
    'motorway_link':  ((7, 1), 1, 3),
    'trunk':          ((6, 4), 3, 4),
    'trunk_link':     ((6, 4), 3, 4),
    'primary':        ((6, 5), 4, 5),
    'primary_link':   ((6, 5), 4, 5),
    'secondary':      ((6, 5), 5, 6),
    'secondary_link': ((6, 5), 5, 6),
    'tertiary':       ((5, 8), 7, 7),
    'tertiary_link':  ((5, 8), 7, 7),
    'unclassified':   ((5, 8), 7, 7),
    'residential':    ((5, 8), 7, 7),
    'living_street':  ((5, 8), 7, 7),
    'service':        ((4, 9), 7, 7),
    'track':          ((4, 9), 7, 7),
}
DEFAULT = ((5, 8), 7, 7)

def layer_class(hw, level, layer=None):
    """(layer, class) for one OSM highway value at level 1 or 2. layer overrides the layer,
    for callers that still want everything in one band."""
    (l1, c1), c2, _ = CLASSES.get(hw, DEFAULT)
    lay, cls = (l1, c1) if level == 1 else (7 if c2 in (1, 2) else 6, c2)
    return (layer if layer is not None else lay), cls

def restyled(styles):
    """Per link, whether its header carries a style word: the first link always, a later
    one only where the word changes, as on every factory chain."""
    return [n == 0 or styles[n] != styles[n - 1] for n in range(len(styles))]

def shape_words(chain, nodes, name_off=0xffff, level=1, cls=None, styles=None):
    """chain: [(link id, from node, to node, [inner points])]. The start node as
    (x | 0x0800, y | 0x2800), then each link's header and its points, and one trailer word
    at level 1. cls is the road class in the top nibble of word 2, which is what the
    renderer widens by. styles: the level-1 style word per link (the posted limit), STYLE
    for all when None.

    The start y's 0x2000 means "a style word follows". Where the style changes along the
    chain, the link end before the change carries 0x2000 on y too and the next link header
    opens with the new word. Factory Norway and northern England do it this way on all
    35,511 restyled links, and never repeat an unchanged word.

    Level-1 x flags, counted over the factory tiles (docs/GEOMETRY.md):

        0x4000  first point of a link
        0x2000  in the first link
        0x1000  the link goes on past this point
        0x0800  this point does not end a link

    A link end inside the chain clears 0x1000 and carries 0x0800 on its y instead; the
    shape's last point keeps 0x1000 and a plain y. The firmware reads a clear 0x0800 as
    "the link ends here" and takes the two words after it for a link header, so a build
    that left it clear on inner points ended every link at its first point and drew a fan
    of crossing lines. Tromso sets it on all 30,690 points that do not end a link and
    clears it on all 13,338 that do, over 7,520 shapes, with no exceptions. 0x0c00 on a
    link end's y is an earlier reading of the same mark: bit 10 belongs to the coordinate,
    so that form drew the shape off the parcel.

    Level 2 has no value word and no trailer, and writes the style word on the first link
    only. Its first-of-link flag is 0x4000 on a link opened by a style word and 0x2000 on
    one that is not; 0x1000 and 0x0800 mean what they do at level 1. Counted on 102,671
    level-2 shapes of B58R060R and B50R060R (214-062), no exceptions in the forms written
    here."""
    if cls is None: cls = 8 if level == 1 else 7
    if styles is None or level != 1: styles = [STYLE] * len(chain)
    fresh = restyled(styles)
    if level == 1: out = [cls << 12, 0x0004, name_off, 0x8004, 0x0000]   # word 4: word offset of the name entry, 0xffff = none
    else: out = [cls << 12, 0x0000, name_off, 0x0000, 0x0000]
    sx, sy = nodes[chain[0][1]]
    out += [0x0800 | sx, 0x2800 | sy]
    npts = 1
    for n, (lid, a, b, inner) in enumerate(chain):
        if level == 1: out += [styles[n], 0x0100, lid] if fresh[n] else [0x0100, lid]
        else: out += [0x101e, lid] if n == 0 else [lid]
        pts = list(inner) + [nodes[b]]
        for k, (x, y) in enumerate(pts):
            first = k == 0; link_end = k == len(pts) - 1; chain_end = n == len(chain) - 1
            if level == 1:
                xf = ((0x4000 if first else 0) | (0x2000 if first and n == 0 else 0)
                      | (0 if link_end and not chain_end else 0x1000)
                      | (0 if link_end else 0x0800))
                if link_end and not chain_end: y |= 0x2800 if fresh[n + 1] else 0x0800
            else:
                # a link opened by a style word starts 0x4000, one without 0x2000
                xf = ((0x4000 if n == 0 else 0x2000) if first else 0) \
                     | (0 if link_end and not chain_end else 0x1000) | (0 if link_end else 0x0800)
                if link_end and not chain_end: y |= 0x0800
            out += [xf | x, y]; npts += 1
    if level == 1: out += [0x0000]
    body = [0x8000 | npts] + out
    return [len(body) + 1] + body

def point_words(chain, level=1, styles=None):
    """[(offset in the shape, link id, role)] for the x word of every point shape_words
    writes. role: 'start' (the shape's first point), 'end' (a link's last point) or
    'vertex' (inside a link). start and end are graph nodes."""
    fresh = restyled(styles) if styles and level == 1 else [n == 0 and level == 1 for n in range(len(chain))]
    out = [(7, chain[0][0], 'start')]
    q = 9
    for n, (lid, a, b, inner) in enumerate(chain):
        q += 3 if fresh[n] else 2
        for k in range(len(inner) + 1):
            out.append((q, lid, 'end' if k == len(inner) else 'vertex')); q += 2
    return out

def item_words(shape, level=1, name_off=0xffff, country=None):
    """Names sub-table item for a shape: 0x8000, its length in words, 0x8000 | 2*(points-2),
    6, 0x0bc0, the ISO 3166 country code, then constants. Level 2 items end with (1, k, name
    offset), k = 1 named, 0 unnamed. Word 5 is the country on every factory item read (578
    Norway and 752 Sweden on B58R060R, 208, 276, 528 on B50R060R); until 19 Oct 2026 this
    wrote 380 (Italy) at level 1 and 250 (France) at level 2."""
    assert country, 'item_words needs the country code'
    npts = shape[1] & 0x3fff   # word 1 is 0x8000 | points
    if level == 1: return [0x8000, 9, 0x8000 | (2 * max(0, npts - 2)), 6, 0x0bc0, country, 0xe, 8, 0xd]
    named = name_off != 0xffff
    return [0x8000, 9, 0x8000 | (2 * max(0, npts - 2)), 6, 0x0bc0, country, 1, 1 if named else 0, name_off if named else 0]

def build_record(level, y0, x0, row, col, nodes, chains, layer=None, name_off=0xffff, name_offs=None, hws=None,
                 styles=None, country=None):
    """name_offs: per-chain name entry offsets (words), overriding name_off. hws: per-chain
    OSM highway value, which picks the layer and class. styles: per chain, the level-1 style
    word of each link (style_word), or None for no posted limit anywhere. Level 1 has twelve layer pairs and a
    pad word, so its data starts at dword 13; level 2 has eleven pairs and starts at 11.
    Shapes are grouped by layer and each layer's pair gives (offset in dwords, count).
    country: ISO 3166 numeric, header word 20 and every names item; word 20 is the parcel's
    country on every factory shape record read, both levels (B58R060R, B50R060R)."""
    assert country, 'build_record needs the country code'
    if hws is None: hws = [None] * len(chains)
    lc = [layer_class(hw, level, layer) for hw in hws]
    if styles is None: styles = [None] * len(chains)
    shapes = [shape_words(c, nodes, name_offs[i] if name_offs else name_off, level, lc[i][1], styles[i])
              for i, c in enumerate(chains)]
    if level == 1: unit = [0x0000] + [0xffff, 0x0000] * 12 + [0x0000]; first = 1
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
    per_row, per_col = {1: (32, 32), 2: (8, 8)}[level]
    code = clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)
    nlinks = sum(len(c) for c in chains)
    tail = [0x0004, 0x0001, 0xffff, 0x0000, 0xffff, 0x0000, 0xffff, 0x0000, ((len(shapes) + nlinks) & 0xff) << 8 | 1, 0x0000]
    if level == 1:
        w = [code[0], code[1], 0x00fa, 0x347a, 0x2aef, 0, 0, 11, len(unit) // 2, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0,
             11 + len(unit) // 2, 5, 0x8104, country, 0]
        plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit) + struct.pack('>10H', *tail)
    else:
        w = [code[0], code[1], 0x00fa, 0xffff, 0xffff, 0, 0, 11, len(unit) // 2, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0,
             0xffff, 0, 0, country, 0]
        plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8); comp = c.compress(plain) + c.flush()
    if len(comp) % 2: comp += b'\0'
    build_record.last_items = [item_words(shapes[i], level, (name_offs[i] if name_offs else name_off), country)
                               for i in order]
    # where each chain landed, in record words: the address files point at these
    build_record.last_index = dict(code=code, unit=2 * w[7], shapes=[
        dict(chain=i, at=2 * w[7] + at[i], layer=lc[i][0], length=len(shapes[i]),
             points=[(2 * w[7] + at[i] + o, lid, role) for o, lid, role in point_words(chains[i], level, styles[i])])
        for i in sorted(at)])
    return plain, comp

def parse_record(plain):
    """Shapes of a level-1 0x00fa record, read with the factory's grammar (REPORT-kanren):
    length word flags 0x2000 / 0x4000 add one / two header words to the base seven; a
    point y with 0x2000 is followed by a style word, one with 0x1000 by an extra word
    between val and id; an x with 0x1000 clear ends the link. Returns (code, unit start,
    [dict(at, length, layer, name, points={x word: (role, link id)}, links, styles, ok)]);
    styles is the style word in force on each link."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    assert w[2] == 0x00fa, hex(w[2])
    g = 2 * w[7]
    shapes = []
    for li in range(12):
        off, cnt = w[g + 1 + 2 * li], w[g + 2 + 2 * li]
        if off == 0xffff: continue
        p = g + 2 * off
        for _ in range(cnt):
            if p >= len(w): break
            L = w[p] & 0x1fff
            h = 7 + (1 if w[p] & 0x2000 else 0) + (2 if w[p] & 0x4000 else 0)
            end = min(p + L - 1, len(w) - 1)
            pts = {p + h: ('start', None)}
            q = p + h + 2; prev_y = w[p + h + 1]; links = []; styles = []; style = None
            while q + 2 < end:
                if prev_y & 0x2000: style = w[q]; q += 1
                styles.append(style)
                q += 1                                  # val
                if prev_y & 0x1000: q += 1
                lid = w[q]; q += 1; links.append(lid)
                if pts[p + h][1] is None: pts[p + h] = ('start', lid)
                while q + 1 <= end and q + 1 < len(w):
                    x, y = w[q], w[q + 1]; q += 2
                    last = not x & 0x1000 or q >= end
                    pts[q - 2] = ('end' if last else 'vertex', lid)
                    prev_y = y
                    if not x & 0x1000: break
            shapes.append(dict(at=p, length=L, layer=li, name=w[p + 4], points=pts, links=links, styles=styles,
                               ok=q == end and len(pts) == (w[p + 1] & 0xff)))
            p += L
    return (w[0], w[1]), g, shapes

def names_section(labels, items):
    """Names section holding road-label entries (in order) and one sub-table item per shape.
    A label is the shipped `1=NAME;2=REF` form, either part on its own if the other is
    missing. Returns (bytes, {label: word offset})."""
    import struct as _s
    body = b''; offs = {}
    for name in labels:
        text = name.encode('utf-8')
        if len(text) % 2: text += b'\0'
        offs[name] = (24 + len(body)) // 2
        body += _s.pack('>HHHHH', 0x00ff, 0x2000, 0, 0, len(text)) + text
    while len(body) % 4: body += b'\0'
    sub = _s.pack('>HH', 8, len(items)) + b'\xff\xff\x00\x00' * 3 + b''.join(_s.pack('>9H', *it) for it in items)
    while len(sub) % 4: sub += b'\0'
    hdr = _s.pack('>HH', 6, len(labels)) + b'\xff\xff\x00\x00' + _s.pack('>HH', (24 + len(body)) // 4, len(items)) + b'\xff\xff\x00\x00' * 3
    return hdr + body + sub, offs

def chains_from_links(links, way_of, chain=False):
    """One shape per link unless chain: then consecutive links of one way form a chain.
    links [(i, j, length)] with ids 1.., way_of[lid] the way id."""
    chains = []
    for lid, (i, j, _) in enumerate(links, start=1):
        if chain and chains and way_of.get(lid) == way_of.get(lid - 1) and chains[-1][-1][2] == i:
            chains[-1].append((lid, i, j, []))
        else:
            chains.append([(lid, i, j, [])])
    return chains
