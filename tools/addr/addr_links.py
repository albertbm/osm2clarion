# SPDX-License-Identifier: GPL-3.0-or-later
"""Match addresses to the drawn road links of a shape index (rd_build.py --shape-index).

An entry is one street of one town: dict(street, name, town, addresses, anchor, low, lat, lon).
`pick` finds the link its record points at, `address_ranges` splits its house numbers into
one range per link and side, and `link_refs` writes the reference words.
"""
import collections
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import addre_io  # noqa: E402
import frames  # noqa: E402
import shape_index  # noqa: E402

NAME_REACH = 1500.0       # metres from the address centroid to a road of the street's name
ACCESS_REACH = 500.0      # metres to any road, for a street with no road of its own name


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


class Links:
    """Every link of every level-1 drawn shape, by street name and by 0.01-degree cell."""

    def __init__(self, records):
        self.byname = collections.defaultdict(list)
        self.grid = collections.defaultdict(list)
        for R in records:
            for S in R['shapes']:
                pts = S['points']
                nm = shape_index.label_name(S['label']).upper() if S['label'] else ''
                for L in S['links']:
                    js = [j for j, P in enumerate(pts) if P['link'] == L['id']]
                    j0 = js[0] if pts[js[0]]['role'] == 'start' else js[0] - 1
                    item = (R, S, L, pts[j0:js[-1] + 1])
                    if nm: self.byname[nm].append(item)
                    for P in item[3]:
                        self.grid[(int(P['lon'] * 100), int(P['lat'] * 100))].append(item)

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


def address_ranges(links, e):
    cands = links.byname.get(e['name'], ())
    if not cands:
        return [], list(e['addresses'])
    matched = []
    for a in e['addresses']:
        p = (a['lon'], a['lat'])
        choices = [(project(p, c[3]), c) for c in cands]
        (dist, along, side), c = min(choices, key=lambda x: x[0][0])
        matched.append((c, side, along, dist, a))
    grouped = collections.defaultdict(list)
    for c, side, along, dist, a in matched:
        if a['num'].isdigit() and int(a['num']):
            R, S, L, run = c
            grouped[(R['code'][0], R['code'][1], S['at'], L['id'], side)].append((along, dist, int(a['num']), a, c))
    out = []
    for key, rows in grouped.items():
        R, S, L, run = rows[0][4]
        nums = [r[2] for r in rows]
        mean_n = sum(nums) / len(nums); mean_a = sum(r[0] for r in rows) / len(rows)
        reverse = sum((r[2] - mean_n) * (r[0] - mean_a) for r in rows) < 0
        first, last = (run[-1], run[0]) if reverse else (run[0], run[-1])
        k, i = first['word'] - R['unit'], last['word'] - R['unit']
        side = -key[-1] if reverse else key[-1]
        flag = 0x54 if side > 0 else 0x58
        refs = addre_io.street_refs(k, i, first['role'] != 'vertex', last['role'] != 'vertex')
        out.append(dict(lo=min(nums), hi=max(nums), side='left' if side > 0 else 'right', side_flag=flag,
                        la=R['code'][0], lo_word=R['code'][1], refs=refs, shape_at=S['at'], link=L['id'],
                        way=L['way'], label=S['label'], addresses=len(rows), max_metres=round(max(r[1] for r in rows), 1),
                        points=[dict(num=r[2], lat=r[3]['lat'], lon=r[3]['lon'], along=r[0]) for r in rows]))
    return sorted(out, key=lambda r: (r['la'], r['lo_word'], r['shape_at'], r['link'], r['side_flag'], r['lo'])), []


def pick(links, e):
    """(how, distance, (R, S, L, run)) for one entry, or None."""
    cands = [(run_dist(e['anchor'], c[3]), c) for c in links.byname.get(e['name'], ())]
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


def link_refs(e, how, d, c):
    R, S, L, run = c
    first, last = run[0], run[-1]
    if metres(e['low'], (last['lon'], last['lat'])) < metres(e['low'], (first['lon'], first['lat'])):
        first, last = last, first
    k, i = first['word'] - R['unit'], last['word'] - R['unit']
    refs = addre_io.street_refs(k, i, first['role'] != 'vertex', last['role'] != 'vertex')
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
    return [(units(folded[p:]), p > 0) for p in starts if folded[p].isalnum()]


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
    """A street's odd and even numbers as two whole-street ranges."""
    odd = sorted(n for n in nums if n % 2)
    even = sorted(n for n in nums if not n % 2)
    out = b''
    if odd:
        out += addre_io.house_item(odd[0], odd[-1], addre_io.SIDE_ODD)
    if even:
        out += addre_io.house_item(even[0], even[-1], addre_io.SIDE_EVEN)
    return out


def addre_mixed(ents, ladder_of, word_keys, prefix, df, area, ntier, kw=1, fmt=None, shorten=False):
    """(file, keys, name blocks) of an ADDRE file of entries sorted by key.

    An entry with e['ranges'] gets one row per range (continuation flag 0x8000 past the
    first), any other a generic row to e['link']. word_keys(name) gives [(key, is a later
    word)]. `shorten` cuts the keys as shortest_keys does."""
    cr = 8 + kw + (4 if kw == 2 else 3)
    names, idx_of, range_idx_of = [], {}, {}
    for i, e in enumerate(ents):
        if 'ranges' in e:
            idxs = []
            for j, r in enumerate(e['ranges']):
                idxs.append(len(names))
                names.append(addre_io.name_record(e['name'] if j == 0 else '', r['la'], r['lo_word'],
                                                  ladder_of(e) if j == 0 else (),
                                                  addre_io.house_item(r['lo'], r['hi'], r['side_flag']),
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
