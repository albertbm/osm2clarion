# SPDX-License-Identifier: GPL-3.0-or-later
"""Match addresses to the drawn road links of a shape index (rd_build.py --shape-index).

An entry is one street of one town: dict(street, name, town, addresses, anchor, low, lat, lon).
`pick` finds the link its record points at, `address_ranges` splits its house numbers into
one range per link, holding both sides of the road as the factory does, and `link_refs`
writes the reference words.

A tile cuts a link at every parcel line it crosses, and the index lists each piece as a link
of its own. A record runs from node to node as the factory's do, over every piece, with a
hop group at each line (`Links.whole`, `chain_refs`).
"""
import collections
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import addre_io  # noqa: E402
import frames  # noqa: E402
import shape_index  # noqa: E402

NAME_REACH = 1500.0       # metres from the address centroid to a road of the street's name
ACCESS_REACH = 500.0      # metres to any road, for a street with no road of its own name
CELL = (0.1, 0.05)        # lon, lat degrees of a cell of the by-name grid, about 5 km


def metres(a, b):
    m = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111320 * m, (b[1] - a[1]) * 111320)


def seg_dist(p, a, b):
    kx = 111320 * math.cos(math.radians(p[1])); ky = 111320
    ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky
    bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    L = bx * bx + by * by
    t = 0 if L == 0 else max(0, min(1, -(ax * bx + ay * by) / L))
    return math.hypot(ax + bx * t, ay + by * t)


HOP = {(1, 0): 0x40, (0, 1): 0x50, (-1, 0): 0x60, (0, -1): 0x70}    # (rows north, columns east): tag


def parcel_of(R):
    """(row, column) of a record's parcel on the level-1 grid."""
    lon, lat, dlon, dlat = R['box']
    return round(lat / dlat), round(lon / dlon)


def line_point(R, P):
    """Where a point sits on the parcel lines, as grid units shared by the parcels either
    side, or None for a point inside its parcel. A record with no box has no lines."""
    if not R.get('box'):
        return None
    lon, lat, dlon, dlat = R['box']
    x, y = round((P['lon'] - lon) / dlon * 2048), round((P['lat'] - lat) / dlat * 2048)
    if x not in (0, 2047) and y not in (0, 2047):
        return None
    row, col = parcel_of(R)
    return col * 2048 + (2048 if x == 2047 else x), row * 2048 + (2048 if y == 2047 else y)


def run_metres(run):
    return sum(metres((a['lon'], a['lat']), (b['lon'], b['lat'])) for a, b in zip(run, run[1:]))


class Links:
    """Every link of every level-1 drawn shape, by street name and by 0.01-degree cell.
    named_only keeps only labelled shapes and no cell grid, for a whole country, where only
    a road of the street's own name is ever used."""

    def __init__(self, records, named_only=False):
        self.byname = collections.defaultdict(list)
        self.grid = collections.defaultdict(list)
        self.named = collections.defaultdict(list)
        self.items = []
        self.cut = []         # links left out: no record can follow them over a parcel line
        self._whole = None
        for R in records:
            self.add(R, named_only)

    def whole(self, item):
        """The link a piece is part of, node to node: dict(parts, start, ok, why). parts are
        (piece, drawn the way the link runs) in order, most of the link's length drawn
        forwards; start maps a piece to the metres before it. ok is False when no factory
        record could follow the link: a piece that stops on a parcel line, or a step to a
        diagonal parcel."""
        if self._whole is None:
            self._whole, self._twins = {}, collections.defaultdict(list)
            for it in self.items:
                for end in (0, -1):
                    at = line_point(it[0], it[3][end])
                    if at:
                        self._twins[(it[2]['way'],) + at].append((it, end))
        if id(item) in self._whole:
            return self._whole[id(item)]
        parts, seen, why = [(item, True)], {id(item)}, None
        for back in (False, True):
            while True:
                it, fwd = parts[0] if back else parts[-1]
                at = line_point(it[0], it[3][0 if fwd == back else -1])
                if at is None:      # a node
                    break
                over = [t for t in self._twins[(it[2]['way'],) + at] if t[0] is not it]
                if len(over) != 1 or id(over[0][0]) in seen:
                    why = 'stops on a parcel line'
                    break
                seen.add(id(over[0][0]))
                nxt = (over[0][0], (over[0][1] == 0) != back)
                parts.insert(0, nxt) if back else parts.append(nxt)
        size = {id(it): run_metres(it[3]) for it, _ in parts}
        if 2 * sum(size[id(it)] for it, fwd in parts if fwd) < sum(size.values()):
            parts = [(it, not fwd) for it, fwd in reversed(parts)]
        for (a, _), (b, _) in zip(parts, parts[1:]):
            (r0, c0), (r1, c1) = parcel_of(a[0]), parcel_of(b[0])
            if (r1 - r0, c1 - c0) not in HOP:
                why = why or 'crosses a parcel corner'
        start, before = {}, 0.0
        for it, _ in parts:
            start[id(it)] = before
            before += size[id(it)]
        chain = dict(parts=parts, start=start, size=size, ok=why is None, why=why)
        for it, _ in parts:
            self._whole[id(it)] = chain
        return chain

    def add(self, R, named_only=False):
        for S in R['shapes']:
            pts = S['points']
            nm = shape_index.label_name(S['label']).upper() if S['label'] else ''
            if named_only and not nm:
                continue
            for L in S['links']:
                js = [j for j, P in enumerate(pts) if P['link'] == L['id']]
                j0 = js[0] if pts[js[0]]['role'] == 'start' else js[0] - 1
                item = (R, S, L, pts[j0:js[-1] + 1])
                self.items.append(item)
                self._whole = None
                if nm:
                    self.byname[nm].append(item)
                    cells = set()
                    for a, b in zip(item[3], item[3][1:]):    # every cell a segment's box touches
                        for cx in range(int(min(a['lon'], b['lon']) / CELL[0]), int(max(a['lon'], b['lon']) / CELL[0]) + 1):
                            for cy in range(int(min(a['lat'], b['lat']) / CELL[1]), int(max(a['lat'], b['lat']) / CELL[1]) + 1):
                                cells.add((cx, cy))
                    for c in sorted(cells):
                        self.named[(nm, c)].append(item)
                if not named_only:
                    for P in item[3]:
                        self.grid[(int(P['lon'] * 100), int(P['lat'] * 100))].append(item)

    def named_near(self, name, lons, lats, reach):
        """The links of a name with a segment in a cell within `reach` metres of the points' box,
        in the order byname holds them."""
        m = reach / 111320
        k = m / math.cos(math.radians(min(max(abs(min(lats)), abs(max(lats))), 89)))
        seen = set()
        for cx in range(int((min(lons) - k) / CELL[0]), int((max(lons) + k) / CELL[0]) + 1):
            for cy in range(int((min(lats) - m) / CELL[1]), int((max(lats) + m) / CELL[1]) + 1):
                seen.update(id(it) for it in self.named.get((name, (cx, cy)), ()))
        return [it for it in self.byname.get(name, ()) if id(it) in seen]

    def near(self, p, reach):
        r = int(math.ceil(reach / 400)) + 1
        cx, cy = int(p[0] * 100), int(p[1] * 100)
        seen = {}
        for dx in range(-2 * r, 2 * r + 1):
            for dy in range(-r, r + 1):
                for it in self.grid.get((cx + dx, cy + dy), ()):
                    seen[id(it)] = it
        return list(seen.values())


def run_dist(p, run):
    return min(seg_dist(p, (a['lon'], a['lat']), (b['lon'], b['lat'])) for a, b in zip(run, run[1:]))


def project(p, run):
    best = None
    before = 0.0
    for a, b in zip(run, run[1:]):
        kx = 111320 * math.cos(math.radians(p[1])); ky = 111320
        ax, ay = (a['lon'] - p[0]) * kx, (a['lat'] - p[1]) * ky
        dx, dy = (b['lon'] - a['lon']) * kx, (b['lat'] - a['lat']) * ky
        size = dx * dx + dy * dy
        t = 0 if size == 0 else max(0, min(1, -(ax * dx + ay * dy) / size))
        dist = math.hypot(ax + t * dx, ay + t * dy)
        cross = dx * -ay - dy * -ax
        along = before + t * math.sqrt(size)
        candidate = (dist, along, 1 if cross >= 0 else -1)
        if best is None or candidate[0] < best[0]:
            best = candidate
        before += math.sqrt(size)
    return best


def match_near(cands, addresses, reach):
    """What project() picks over every candidate for each address, as numpy does it, keeping
    only addresses within `reach` metres. ([(c, side, along, dist, a)], [unmatched address])."""
    ax, ay, bx, by, owner = [], [], [], [], []
    for ci, c in enumerate(cands):
        for a, b in zip(c[3], c[3][1:]):
            ax.append(a['lon']); ay.append(a['lat']); bx.append(b['lon']); by.append(b['lat'])
            owner.append(ci)
    if not ax:
        return [], list(addresses)
    ax, ay, bx, by, owner = map(np.array, (ax, ay, bx, by, owner))
    out, missed = [], []
    step = max(1, 2_000_000 // len(ax))
    for s0 in range(0, len(addresses), step):
        part = addresses[s0:s0 + step]
        px = np.array([a['lon'] for a in part])[:, None]
        py = np.array([a['lat'] for a in part])[:, None]
        kx = 111320 * np.cos(np.radians(py)); ky = np.full_like(kx, 111320)
        vx, vy = (ax - px) * kx, (ay - py) * ky
        dx, dy = (bx - ax) * kx, (by - ay) * ky
        size = dx * dx + dy * dy
        with np.errstate(invalid='ignore', divide='ignore'):
            t = np.where(size == 0, 0, np.clip(-(vx * dx + vy * dy) / size, 0, 1))
        dist = np.hypot(vx + t * dx, vy + t * dy)
        j = np.argmin(dist, axis=1)
        for i, a in enumerate(part):
            jj = j[i]
            if dist[i, jj] > reach:
                missed.append(a)
                continue
            ci = owner[jj]
            first = np.searchsorted(owner, ci)
            seglen = np.sqrt(size[i, first:jj])           # the earlier segments of the same run
            along = float(seglen.sum() + t[i, jj] * math.sqrt(size[i, jj]))
            cross = dx[i, jj] * -vy[i, jj] - dy[i, jj] * -vx[i, jj]
            out.append((cands[ci], 1 if cross >= 0 else -1, along, float(dist[i, jj]), a))
    return out, missed


def address_ranges(links, e, reach=None):
    """(ranges, unmatched addresses): one range per link with a house on it, its items the
    right and left side seen along its references. With `reach`, only roads of the name
    within that many metres of an address count, and an address farther from all of them
    is unmatched."""
    unmatched = []
    if reach is None:
        cands = links.byname.get(e['name'], ())
        if not cands:
            return [], list(e['addresses'])
        matched = []
        for a in e['addresses']:
            p = (a['lon'], a['lat'])
            choices = [(project(p, c[3]), c) for c in cands]
            (dist, along, side), c = min(choices, key=lambda x: x[0][0])
            matched.append((c, side, along, dist, a))
    else:
        adr = list(e['addresses'])
        cands = links.named_near(e['name'], [a['lon'] for a in adr], [a['lat'] for a in adr], reach)
        matched, unmatched = match_near(cands, adr, reach)
        if not matched:
            return [], unmatched
    # a number is one place on its street: of several buildings, the one nearest the road
    nearest = {}
    for m in matched:
        num = m[4]['num']
        if num.isdigit() and int(num) and (int(num) not in nearest or m[3] < nearest[int(num)][3]):
            nearest[int(num)] = m
    grouped = collections.defaultdict(list)
    for num, (c, side, along, dist, a) in nearest.items():
        chain = links.whole(c)
        fwd = dict((id(it), f) for it, f in chain['parts'])[id(c)]
        along = chain['start'][id(c)] + (along if fwd else chain['size'][id(c)] - along)
        grouped[id(chain)].append((along, dist, num, a, side if fwd else -side, chain))
    out = []
    for rows in grouped.values():
        chain = rows[0][5]
        if not chain['ok']:
            R, S, L, _ = chain['parts'][0][0]
            links.cut.append(dict(street=e['name'], town=e.get('town'), la=R['code'][0], lo=R['code'][1],
                                  link=L['id'], way=L['way'], addresses=len(rows), why=chain['why']))
            continue
        # the record runs the way the link's numbers rise; a side may still fall
        turn = -1 if falls([(r[0], r[2]) for r in rows]) else 1
        (R, S, L, _), refs = chain_refs(chain, turn)
        items = []
        for left in (False, True):       # the factory holds the right side first
            part = [r for r in rows if (r[4] * turn > 0) == left]
            if not part:
                continue
            nums = [r[2] for r in part]
            ends = (min(nums), max(nums))[::-1 if falls([(r[0] * turn, r[2]) for r in part]) else 1]
            items.append(dict(side='left' if left else 'right', flag=addre_io.side_byte(left, nums),
                              first=ends[0], last=ends[1], addresses=len(part)))
        nums = [r[2] for r in rows]
        out.append(dict(lo=min(nums), hi=max(nums), items=items,
                        la=R['code'][0], lo_word=R['code'][1], refs=refs, shape_at=S['at'], link=L['id'],
                        way=L['way'], label=S['label'], addresses=len(rows), max_metres=round(max(r[1] for r in rows), 1),
                        points=[dict(num=r[2], lat=r[3]['lat'], lon=r[3]['lon'], along=r[0],
                                     side='left' if r[4] * turn > 0 else 'right') for r in rows]))
    return sorted(out, key=lambda r: (r['la'], r['lo_word'], r['shape_at'], r['link'])), unmatched


def chain_refs(chain, turn=1):
    """(the piece a record starts on, its reference groups) for a whole link run forwards
    (turn 1) or backwards (-1). Each piece gives two groups. The second is tagged 2 where
    the references rise inside the piece and 4 where they fall, 3 and 5 on the last piece;
    the first has the same tag on the first piece and the hop tag on a later one. Every one
    of the factory's 1.5 million pieces reads that way. Flag 0x40 marks a node or a point
    on the line, 0x00 an interior vertex."""
    parts = [(it, fwd == (turn > 0)) for it, fwd in chain['parts'][::turn]]
    refs = []
    for n, ((R, S, L, run), rises) in enumerate(parts):
        first, last = (run[0], run[-1])[::1 if rises else -1]
        tag = 2 if rises else 4
        if n:
            (r0, c0), (r1, c1) = parcel_of(parts[n - 1][0][0]), parcel_of(R)
        refs.append((HOP[(r1 - r0, c1 - c0)] if n else tag, 0x40 if first['role'] != 'vertex' else 0, first['word'] - R['unit']))
        refs.append((tag + (n == len(parts) - 1), 0x40 if last['role'] != 'vertex' else 0, last['word'] - R['unit']))
    return parts[0][0], tuple(refs)


def falls(rows):
    """True when the numbers of [(metres along, number)] get smaller along the way."""
    mean_a = sum(a for a, _ in rows) / len(rows)
    mean_n = sum(n for _, n in rows) / len(rows)
    return sum((n - mean_n) * (a - mean_a) for a, n in rows) < 0


def pick(links, e, local=False):
    """(how, distance, (R, S, L, run)) for one entry, or None. `local` looks the name up in
    the by-name grid, which gives the same answer without walking every road of the name."""
    named = (links.named_near(e['name'], [e['anchor'][0]], [e['anchor'][1]], NAME_REACH) if local
             else links.byname.get(e['name'], ()))
    cands = [(run_dist(e['anchor'], c[3]), c) for c in named]
    cands = [c for c in cands if c[0] <= NAME_REACH]
    if cands:
        d, c = min(cands, key=lambda c: c[0])
        return 'name', d, c
    cands = [(run_dist(e['anchor'], c[3]), c) for c in links.near(e['anchor'], ACCESS_REACH)]
    cands = [c for c in cands if c[0] <= ACCESS_REACH]
    if cands:
        d, c = min(cands, key=lambda c: c[0])
        return 'access', d, c
    return None


def link_refs(links, e, how, d, c):
    """The row of a street with no number on any link: the whole link, from the end nearer
    the street's lowest number. None when no record can follow the link."""
    chain = links.whole(c)
    if not chain['ok']:
        return None
    (a, fa), (b, fb) = chain['parts'][0], chain['parts'][-1]
    first, last = a[3][0 if fa else -1], b[3][-1 if fb else 0]
    turn = -1 if metres(e['low'], (last['lon'], last['lat'])) < metres(e['low'], (first['lon'], first['lat'])) else 1
    (R, S, L, run), refs = chain_refs(chain, turn)
    mid = ((run[0]['lat'] + run[1]['lat']) / 2, (run[0]['lon'] + run[1]['lon']) / 2)
    assert addre_io.parcel_words(*mid) == tuple(R['code']), (e['street'], R['code'])
    home = addre_io.parcel_words(e['lat'], e['lon'])
    return dict(street=e['name'], town=e['town'], la=R['code'][0], lo=R['code'][1], refs=refs, shape_at=S['at'],
                link=L['id'], way=L['way'], label=S['label'], how=how, metres=round(d, 1),
                other_parcel=tuple(home) != tuple(R['code']))


def word_keys(folded, kw=1):
    """[(key, is a later word)] for a folded name: one key per word start, running to the end."""
    def units(t):
        return b''.join(ch.encode('utf-8').ljust(kw, b'\0') for ch in t)
    starts = [0] + [j + 1 for j, c in enumerate(folded) if c == ' ' and j + 1 < len(folded)]
    return [(units(folded[p:]), p > 0) for p in starts if p < len(folded) and folded[p].isalnum()]


def shortest_keys(keys):
    """Sorted keys cut to the first letter that tells each from its neighbours, as a factory trie cuts them.

    1,100 of the 1,133 keys of the QY7 card's ADDRE014 hold one name; the rest are later
    words that several names share."""
    def common(a, b):
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        return n
    out = []
    for i, k in enumerate(keys):
        near = [common(k, keys[j]) for j in (i - 1, i + 1) if 0 <= j < len(keys)]
        out.append(k[:max(near, default=0) + 1])
    return out


def ladder_first(srecs):
    """(rows, pre): each row carries the next row's ladder, and pre the first.

    The unit reads a record's ladder from the 10 bytes before its row."""
    pre = srecs[0][12:22]
    rows = [r[:12] + n[12:22] for r, n in zip(srecs, srecs[1:])] + [srecs[-1][:12] + b'\0' * 10]
    return rows, pre


def house_items(nums):
    """A street's odd and even numbers as two whole-street ranges, for a row that names no
    link per number; which side each lies on is not known."""
    odd = sorted(n for n in nums if n % 2)
    even = sorted(n for n in nums if not n % 2)
    out = b''
    if odd:
        out += addre_io.house_item(odd[0], odd[-1], addre_io.SIDE_RIGHT)
    if even:
        out += addre_io.house_item(even[0], even[-1], addre_io.SIDE_LEFT)
    return out


def addre_mixed(ents, ladder_of, word_keys, prefix, df, area, ntier, kw=1, fmt=None, shorten=False):
    """(file, keys, name blocks) of an ADDRE file of entries sorted by key.

    An entry with e['ranges'] gets one row and one name record per range (continuation flag
    0x8000 past the first), any other a generic row to e['link']. word_keys(name) gives
    [(key, is a later word)]. `shorten` cuts the keys as shortest_keys does."""
    cr = 8 + kw + (4 if kw == 2 else 3)
    names, idx_of, range_idx_of = [], {}, {}
    for i, e in enumerate(ents):
        if 'ranges' in e:
            idxs = []
            for j, r in enumerate(e['ranges']):
                idxs.append(len(names))
                names.append(addre_io.name_record(e['name'] if j == 0 else '', r['la'], r['lo_word'],
                                                  ladder_of(e) if j == 0 else (),
                                                  b''.join(addre_io.house_item(it['first'], it['last'], it['flag'])
                                                           for it in r['items']),
                                                  refs=r['refs'], flags=0x18 if j == 0 else 0x19, fmt=fmt))
            range_idx_of[i] = idxs
        else:
            idx_of[i] = len(names)
            names.append(addre_io.name_record(e['name'], e['link']['la'], e['link']['lo'], ladder_of(e),
                                              house_items(e['nums']), refs=e['link']['refs'], fmt=fmt))
    bykey = collections.defaultdict(list)
    for i, e in enumerate(ents):
        for u, later in word_keys(e['name']):
            bykey[u].append((i, later))
    keys, srecs = [], []
    for u in sorted(bykey):
        start = len(srecs)
        for i, later in sorted(bykey[u], key=lambda h: h[0]):
            e = ents[i]
            if i in range_idx_of:
                for j, name_idx in enumerate(range_idx_of[i]):
                    r = e['ranges'][j]
                    flags = (0x4000 if later else 0) | (0x8000 if j else 0)
                    srecs.append(addre_io.range_record(flags, r['lo'], r['hi'], name_idx, ladder_of(e)))
            else:
                srecs.append(addre_io.street_record(0x4000 if later else 0, idx_of[i], ladder_of(e)))
        keys.append((u, start, len(srecs) - start))
    if shorten:
        assert kw == 1, kw
        keys = [(u, f, n) for u, (_, f, n) in zip(shortest_keys([k[0] for k in keys]), keys)]
    srecs, pre = ladder_first(srecs)
    d, w = frames.addre_hints(names)
    out, blocks = addre_io.assemble(prefix, keys, srecs, names, df, pre, cr, kw, area, ntier,
                                    maxkey_hint=d, namewidth_hint=w, fmt=fmt)
    return out, keys, blocks
