#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn restrictions of an OSM extract as JSON for rd_build.py.

    ./osm_turns.py iceland-latest.osm.pbf turns_iceland.json

Only `type=restriction` relations with a single **node** via member and one `from` and one
`to` way are kept: those are the ones that land on a junction node of the road graph, which
is where the card's regulation records live (KIWI 1.22 10.7.1.2). A way via member means the
restriction spans an intermediate link and needs a path, not a node; those are counted and
dropped. `only_*` restrictions are also counted and dropped: they prohibit every turn
except one, which is an expansion over the node's other links rather than a straight
translation, and doing it wrong would block roads that are open.

Output: [{id, kind, from, to, via: [lon, lat]}, ...].
"""
import collections, json, sys
import osmium

PROHIBIT = {'no_left_turn', 'no_right_turn', 'no_straight_on', 'no_u_turn', 'no_entry', 'no_exit'}


class Pass1(osmium.SimpleHandler):
    """Collect the restrictions and the via node ids they need."""
    def __init__(self):
        super().__init__()
        self.keep = []
        self.want = set()
        self.drop = collections.Counter()

    def relation(self, r):
        if (r.tags.get('type') or '') != 'restriction':
            return
        kind = (r.tags.get('restriction') or r.tags.get('restriction:motorcar')
                or r.tags.get('restriction:hgv') or '')
        frm = [m.ref for m in r.members if m.role == 'from' and m.type == 'w']
        to = [m.ref for m in r.members if m.role == 'to' and m.type == 'w']
        via_n = [m.ref for m in r.members if m.role == 'via' and m.type == 'n']
        via_w = [m for m in r.members if m.role == 'via' and m.type == 'w']
        if kind not in PROHIBIT:
            self.drop['kind ' + (kind or '(none)')] += 1; return
        if via_w:
            self.drop['via is a way'] += 1; return
        if len(via_n) != 1 or len(frm) != 1 or len(to) != 1:
            self.drop['member shape'] += 1; return
        self.keep.append(dict(id=r.id, kind=kind, **{'from': frm[0]}, to=to[0], via=via_n[0]))
        self.want.add(via_n[0])


class Pass2(osmium.SimpleHandler):
    def __init__(self, want):
        super().__init__(); self.want = want; self.loc = {}

    def node(self, n):
        if n.id in self.want and n.location.valid():
            self.loc[n.id] = (n.location.lon, n.location.lat)


if __name__ == '__main__':
    src, dest = sys.argv[1], sys.argv[2]
    p1 = Pass1(); p1.apply_file(src)
    p2 = Pass2(p1.want); p2.apply_file(src)
    out = []
    missing = 0
    for r in p1.keep:
        loc = p2.loc.get(r['via'])
        if loc is None:
            missing += 1; continue
        out.append(dict(id=r['id'], kind=r['kind'], **{'from': r['from']}, to=r['to'], via=list(loc)))
    json.dump(out, open(dest, 'w'))
    print('kept %d restrictions' % len(out))
    if missing: print('  %d dropped: via node not in the extract' % missing)
    for k, v in sorted(p1.drop.items(), key=lambda kv: -kv[1]):
        print('  %d dropped: %s' % (v, k))
