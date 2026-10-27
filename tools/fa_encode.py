#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The road-shape record (kind 0x00fa) in the even pair of a level-1 background directory:
header, a geometry unit of twelve layer pairs, then the shapes, one per drawn link chain."""
import os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_bg

STYLE = 0x101e        # the style word 79,182 of 128,000 shipped level-1 shapes carry: no limit

def style_word(limit, oneway=False):
    """Level-1 style word for a posted limit, or STYLE for none; bit 12 clear marks one-way."""
    w = STYLE if not limit or limit % 5 or not 5 <= limit <= 160 else 0x1020 | (limit // 5 - 1)
    return w & ~0x1000 if oneway else w

def limit_of(style):
    """The posted limit a style word carries, or None."""
    return ((style & 0x1f) + 1) * 5 if style & 0x20 else None

def speed_limit(maxspeed, unit='km/h'):
    """OSM maxspeed as a posted limit in km/h or mph, or None when it isn't a multiple of 5."""
    if not maxspeed: return None
    parts = str(maxspeed).strip().lower().split(';')[0].split()
    try: v = float(parts[0])
    except (ValueError, IndexError): return None
    src = 'mph' if len(parts) > 1 and parts[1] == 'mph' else 'km/h'
    if src != unit: v = v / 1.609344 if unit == 'mph' else v * 1.609344
    v = round(v)
    if src != unit: v = 5 * round(v / 5)
    return v if v % 5 == 0 and 5 <= v <= 160 else None

# OSM highway -> (level-1 layer, class), level-2 class, level-3 class; class follows road standard, not route prefix
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
    """(layer, class) for one OSM highway value at level 1 or 2."""
    (l1, c1), c2, _ = CLASSES.get(hw, DEFAULT)
    lay, cls = (l1, c1) if level == 1 else (7 if c2 in (1, 2) else 6, c2)
    return (layer if layer is not None else lay), cls

def restyled(styles):
    """Per link, whether its header carries a style word: the first always, a later one only where it changes."""
    return [n == 0 or styles[n] != styles[n - 1] for n in range(len(styles))]

def shape_words(chain, nodes, name_off=0xffff, level=1, cls=None, styles=None, dirs=None):
    """Section-1 shape words for one link chain: start point, then each link's header and
    points, with x flags 0x4000/0x2000/0x1000/0x0800 marking link starts, style changes and
    ends (docs/GEOMETRY.md), and the one-way sense OR'd into bits 15-14 of y."""
    if cls is None: cls = 8 if level == 1 else 7
    if styles is None or level != 1: styles = [STYLE] * len(chain)
    if dirs is None: dirs = [0] * len(chain)
    fresh = restyled(styles)
    def dirbits(n):
        d = dirs[n]
        return 0x4000 if d > 0 else 0x8000 if d < 0 else 0
    if level == 1: out = [cls << 12, 0x0004, name_off, 0x8004, 0x0000]   # word 4: word offset of the name entry, 0xffff = none
    else: out = [cls << 12, 0x0000, name_off, 0x0000, 0x0000]
    sx, sy = nodes[chain[0][1]]
    out += [0x0800 | sx, 0x2800 | sy | dirbits(0)]
    npts = 1
    for n, (lid, a, b, inner) in enumerate(chain):
        if level == 1: out += [styles[n], 0x0100, lid] if fresh[n] else [0x0100, lid]
        else: out += [style_word(None, oneway=dirs[n] != 0), lid] if n == 0 else [lid]
        pts = list(inner) + [nodes[b]]
        for k, (x, y) in enumerate(pts):
            first = k == 0; link_end = k == len(pts) - 1; chain_end = n == len(chain) - 1
            if level == 1:
                xf = ((0x4000 if first else 0) | (0x2000 if first and fresh[n] else 0)
                      | (0 if link_end and not chain_end else 0x1000)
                      | (0 if link_end else 0x0800))
                if link_end and not chain_end: y |= 0x2800 if fresh[n + 1] else 0x0800
            else:
                xf = ((0x4000 if n == 0 else 0x2000) if first else 0) \
                     | (0 if link_end and not chain_end else 0x1000) | (0 if link_end else 0x0800)
                if link_end and not chain_end: y |= 0x0800
            out += [xf | x, y | dirbits(n)]; npts += 1
    if level == 1: out += [0x0000]
    body = [0x8000 | npts] + out
    return [len(body) + 1] + body

def point_words(chain, level=1, styles=None):
    """[(word offset, link id, role)] for every point shape_words writes; role is start, end or vertex."""
    fresh = restyled(styles) if styles and level == 1 else [n == 0 and level == 1 for n in range(len(chain))]
    out = [(7, chain[0][0], 'start')]
    q = 9
    for n, (lid, a, b, inner) in enumerate(chain):
        q += 3 if fresh[n] else 2
        for k in range(len(inner) + 1):
            out.append((q, lid, 'end' if k == len(inner) else 'vertex')); q += 2
    return out

def item_words(shape, level=1, name_off=0xffff, country=None):
    """Names sub-table item for a shape; word 5 is the ISO 3166 country code."""
    assert country, 'item_words needs the country code'
    npts = shape[1] & 0x3fff   # word 1 is 0x8000 | points
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
    """A built record's tail: (item count, items). Level 1 only."""
    w = struct.unpack('>%dH' % (len(plain) // 2), plain)
    off, cnt = w[17], w[18]
    tail = w[2 * off: 2 * off + 2 * cnt]
    return tail[1], list(tail[8:8 + tail[1]])

def build_record(level, y0, x0, row, col, nodes, chains, layer=None, name_off=0xffff, name_offs=None, hws=None,
                 styles=None, country=None, dirs=None):
    """A level-1 or level-2 road-shape record and its compressed form, shapes grouped by layer."""
    assert country, 'build_record needs the country code'
    if hws is None: hws = [None] * len(chains)
    lc = [layer_class(hw, level, layer) for hw in hws]
    if styles is None: styles = [None] * len(chains)
    if dirs is None: dirs = [None] * len(chains)
    shapes = [shape_words(c, nodes, name_offs[i] if name_offs else name_off, level, lc[i][1], styles[i], dirs[i])
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
    items = tail_items(len(shapes) + nlinks)
    tail = [0x0004, len(items), 0xffff, 0x0000, 0xffff, 0x0000, 0xffff, 0x0000] + items
    if len(tail) % 2: tail.append(0)
    if level == 1:
        w = [code[0], code[1], 0x00fa, 0x347a, 0x2aef, 0, 0, 11, len(unit) // 2, 0xffff, 0, 0xffff, 0, 0xffff, 0, 0xffff, 0,
             11 + len(unit) // 2, len(tail) // 2, 0x8104, country, 0]
        plain = struct.pack('>22H', *w) + struct.pack('>%dH' % len(unit), *unit) + struct.pack('>%dH' % len(tail), *tail)
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
        dict(chain=i, at=2 * w[7] + at[i], layer=lc[i][0], cls=lc[i][1], length=len(shapes[i]),
             points=[(2 * w[7] + at[i] + o, lid, role) for o, lid, role in point_words(chains[i], level, styles[i])])
        for i in sorted(at)])
    return plain, comp

def parse_record(plain, level=1):
    """Shapes of a 0x00fa record, parsed with the factory's own grammar (REPORT-kanren).
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
                while q + 1 <= end and q + 1 < len(w):
                    x, y = w[q], w[q + 1]; q += 2
                    last = not x & 0x1000 or q >= end
                    pts[q - 2] = ('end' if last else 'vertex', lid)
                    prev_y = y; cur = (x & 0x7ff, y & 0x7ff)
                    if not x & 0x1000: break
                ends.append((first, cur))
            shapes.append(dict(at=p, length=L, layer=li, cls=w[p + 2] >> 12, name=w[p + 4], points=pts, links=links,
                               styles=styles, ends=ends, ok=q == end and len(pts) == (w[p + 1] & 0xff)))
            p += L
    return (w[0], w[1]), g, shapes

def names_section(labels, items):
    """Names section: road-label entries plus one sub-table item per shape. Returns (bytes, {label: word offset})."""
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
    """One shape per link, or (chain=True) consecutive links of one way merged into a chain."""
    chains = []
    for lid, (i, j, _) in enumerate(links, start=1):
        if chain and chains and way_of.get(lid) == way_of.get(lid - 1) and chains[-1][-1][2] == i:
            chains[-1].append((lid, i, j, []))
        else:
            chains.append([(lid, i, j, [])])
    return chains
