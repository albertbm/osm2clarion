# SPDX-License-Identifier: GPL-3.0-or-later
"""Kunta and settlement of each register address, from the card's own area tree and ADDRE file.

A card that keeps its factory area tree has one row per municipality and settlement as they
stood when the card was made. The factory street file says where that tree puts each street,
so it decides here. For each address:

  a. the kunta is the one its municipality number names: the OSM admin_level 8 boundary with
     that ref, matched on its name, Finnish name or Swedish name (the tree spells some in
     Swedish, TAMMERFORS). A kunta merged away since the card was made keeps its own row,
     so where one lies inside the municipality the nearest factory record picks between them;
  b. a street the factory has in one settlement of that kunta goes there;
  c. a street it has in several goes to the settlement of its nearest record of that name;
  d. any other street goes to the settlement of the nearest factory record in the kunta.
     Scored leave-one-street-out on the 2014 QY7 card's Finnish file, d gives the factory's
     settlement for 92.7% of addresses, against 77% for the best rule on OSM places.

The factory's no-settlement marker counts as a settlement in b and c, since the factory
writes it there; d picks a real settlement. Factory positions are level-1 parcel centres, the
precision a record's lat/lon words carry; a record whose words name no whole parcel has no
position and only counts in b and c.
"""
import collections
import os
import sys

import numpy as np
import shapely

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import addre_io  # noqa: E402
import clarion_bg  # noqa: E402
import icode_io  # noqa: E402

MERGED_REACH = 0.1     # degrees of latitude, about 11 km, from a merged kunta's record to an address
KUNTA_PATHS = ('a: municipality number', 'a: merged kunta by position', 'a: kunta by position only')
PATHS = ('b: one factory settlement', 'c: nearest same-name record', 'd: nearest factory record',
         'd: no factory record, marker')


def area_tree(ic, iso, codec):
    """({kunta: (ladder, {settlement: index})}, tier labels) of every row one level above the
    deepest. The settlement count is the factory's no-settlement marker."""
    ordinal = icode_io.iso_ordinal(ic, iso)
    labels = icode_io.iso_entry(ic, ordinal)[1]
    _lang, base = icode_io.area_frames(ic)[0]
    out = {}

    def walk(row, path):
        for r in icode_io.children(ic, base, row):
            if len(path) + 2 < len(labels):
                walk(r, path + [r.idx])
                continue
            name = r.name.decode(codec)
            if name in out:
                raise SystemExit('two kunta rows named %s in country %d' % (name, iso))
            out[name] = (tuple(path + [r.idx]), {c.name.decode(codec): c.idx for c in icode_io.children(ic, base, r)})
    walk(icode_io.read_list(ic, base)[ordinal], [ordinal])
    return out, labels


def folder(fmt):
    """The comparison form of a name: the card's spelling folded as a search key."""
    def fold(s):
        return addre_io.fold_key(fmt.text((s or '').strip().upper()).decode(fmt.codec)).upper()
    return fold


def xy(lat, lon):
    """Degrees of latitude both ways, east-west shrunk by the cosine; nearest-point maths only."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    return (lon - 25.0) * np.cos(np.radians(lat)), lat


def factory_records(addre, fmt, tree):
    """[(kunta, street name, settlement index, lat or None, lon or None)] of a factory ADDRE file."""
    kunta_of = {path: k for k, (path, _) in tree.items()}
    out, cur = [], None
    for r in addre.names:
        p = addre_io.parse_name_record(r, fmt)
        if p['name'] is not None:
            lv = tuple(p['levels'])
            cur = (kunta_of.get(lv[:-1]), p['name'], lv[-1])
        if cur is None or cur[0] is None:
            continue
        try:
            row, col = clarion_bg.parcel_rowcol(1, p['lat'], p['lon'])
            lat, lon = (row + .5) / 48, (col + .5) / 32 - 924
        except AssertionError:
            lat = lon = None
        out.append(cur + (lat, lon))
    return out


class Centres:
    """Unique positions with a Counter of labels each, for nearest-label queries."""

    def __init__(self, items):
        by = collections.defaultdict(collections.Counter)
        for lat, lon, label in items:
            by[(lat, lon)][label] += 1
        self.keys = sorted(by)
        self.counts = [by[k] for k in self.keys]
        if self.keys:
            x, y = xy([k[0] for k in self.keys], [k[1] for k in self.keys])
            self.tree = shapely.STRtree(shapely.points(x, y))

    def __bool__(self):
        return bool(self.keys)

    def nearest(self, lat, lon):
        """The most common label at each point's nearest position; equal distances pool their counts."""
        x, y = xy(lat, lon)
        pairs = self.tree.query_nearest(shapely.points(x, y), all_matches=True)
        pooled = collections.defaultdict(collections.Counter)
        for i, j in zip(*pairs):
            pooled[int(i)].update(self.counts[int(j)])
        return [best(pooled[i]) for i in range(len(x))]


def best(counter):
    """The label with the most records; the lowest wins a tie, so a run gives the same answer twice."""
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


class Settler:
    def __init__(self, tree, records, municipalities, fold):
        """tree from area_tree, records from factory_records, municipalities {number: [names]}."""
        self.tree, self.fold = tree, fold
        self.none = {k: len(s) for k, (_, s) in tree.items()}
        bykey = {fold(k): k for k in tree}
        self.kunta_of = {}
        for num, names in municipalities.items():
            hit = {bykey[fold(n)] for n in names if n and fold(n) in bykey}
            if len(hit) == 1:
                self.kunta_of[int(num)] = hit.pop()
        self.merged = sorted(set(tree) - set(self.kunta_of.values()))
        self.streets = collections.defaultdict(lambda: collections.defaultdict(list))
        located = collections.defaultdict(list)
        for k, name, s, lat, lon in records:
            self.streets[k][fold(name)].append((s, lat, lon))
            if lat is not None:
                located[k].append((lat, lon, s))
        self.located = located
        self.real = {k: Centres([(la, lo, s) for la, lo, s in v if s != self.none[k]]) for k, v in located.items()}

    def merged_home(self, lat, lon, num):
        """{merged kunta: the municipality number most of its record positions fall nearest to}."""
        known = num >= 0
        if not known.any():
            return {}
        lat, lon, num = lat[known], lon[known], num[known]
        tree = shapely.STRtree(shapely.points(*xy(lat, lon)))
        out = {}
        for k in self.merged:
            pts = sorted({(la, lo) for la, lo, _ in self.located.get(k, ())})
            if not pts:
                continue
            # no address that near: the rows cover an area the kunta is not in
            got = tree.query_nearest(shapely.points(*xy([p[0] for p in pts], [p[1] for p in pts])),
                                     max_distance=MERGED_REACH)[1]
            if len(got):
                out[k] = best(collections.Counter(int(num[j]) for j in got))
        return out

    def assign(self, rows):
        """(kunta, settlement index, kunta path, settlement path) per row, rows being dicts with
        lat, lon, svf (municipality number), street and street_swe; and {merged kunta: number}."""
        n = len(rows)
        lat = np.array([r['lat'] for r in rows], float)
        lon = np.array([r['lon'] for r in rows], float)
        num = np.array([int(r['svf']) if str(r['svf']).strip().isdigit() else -1 for r in rows])
        home = self.merged_home(lat, lon, num)
        kunta, khow, how = [None] * n, [KUNTA_PATHS[0]] * n, [None] * n
        everyone = None
        for c in sorted(set(num.tolist())):
            idx = np.nonzero(num == c)[0]
            cand = ([self.kunta_of[c]] if c in self.kunta_of else []) + sorted(k for k, h in home.items() if h == c)
            if len(cand) == 1:
                for i in idx:
                    kunta[i] = cand[0]
                continue
            if cand:
                cen, tag = Centres([(la, lo, k) for k in cand for la, lo, _ in self.located.get(k, ())]), KUNTA_PATHS[1]
            else:
                everyone = everyone or Centres([(la, lo, k) for k, v in self.located.items() for la, lo, _ in v])
                cen, tag = everyone, KUNTA_PATHS[2]
            for i, k in zip(idx, cen.nearest(lat[idx], lon[idx])):
                kunta[i] = k
                if k != self.kunta_of.get(c):
                    khow[i] = tag
        settle = [None] * n
        groups = collections.defaultdict(list)
        for i, r in enumerate(rows):
            st = self.streets[kunta[i]]
            name = next((f for f in (self.fold(r['street']), self.fold(r.get('street_swe'))) if f and f in st), None)
            groups[(kunta[i], name)].append(i)
        for (k, name), idx in sorted(groups.items(), key=lambda g: (g[0][0], g[0][1] or '')):
            idx = np.array(idx)
            if name is None:
                if self.real.get(k):
                    got, tag = self.real[k].nearest(lat[idx], lon[idx]), PATHS[2]
                else:
                    got, tag = [self.none[k]] * len(idx), PATHS[3]
            else:
                recs = self.streets[k][name]
                kinds = {s for s, _, _ in recs}
                if len(kinds) == 1:
                    got, tag = [kinds.pop()] * len(idx), PATHS[0]
                else:
                    got, tag = self._same_name(recs, lat[idx], lon[idx]), PATHS[1]
            for i, s in zip(idx, got):
                settle[i] = s
                how[i] = tag
        return list(zip(kunta, settle, khow, how)), home

    @staticmethod
    def _same_name(recs, lat, lon):
        pos = [(la, lo, s) for s, la, lo in recs if la is not None]
        if not pos:
            return [best(collections.Counter(s for s, _, _ in recs))] * len(lat)
        return Centres(pos).nearest(lat, lon)


def counts(rows, out, fold):
    """{path: (addresses, streets)} for the kunta paths and the settlement paths. A street is a
    kunta and folded name, counted on the path its first address took."""
    res = {}
    for col, paths in ((2, KUNTA_PATHS), (3, PATHS)):
        adr = collections.Counter(o[col] for o in out)
        seen = {}
        for r, o in zip(rows, out):
            seen.setdefault((o[0], fold(r['street'])), o[col])
        st = collections.Counter(seen.values())
        res.update({p: (adr[p], st[p]) for p in paths if adr[p]})
    return res
