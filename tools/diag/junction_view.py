#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Census of junctions by the arms that meet there, from a roads.json.

A helper for the open junction-view question (the QY8 unit draws the wrong picture at some
"keep left/right" calls). It does not predict the picture: what the unit picks it from is not
known yet. It only sorts nodes so candidates can be listed and compared with factory tiles.

  ramp   a *_link arm meets the node, or one ref carries on in a different highway class
  strip  three or more arms, none of the above
  none   fewer than three arms

    junction_view.py roads.json [lat,lon ...]      the named nodes with their arms, or a census
"""
import collections
import json
import math
import sys


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(a[1])),
                      (a[1] - b[1]) * 110540)


def bearing(a, b):
    return math.degrees(math.atan2((b[0] - a[0]) * math.cos(math.radians(a[1])),
                                   b[1] - a[1])) % 360


def base_class(hw):
    return hw[:-5] if hw.endswith('_link') else hw


def load(path):
    ways = json.load(open(path))
    use = collections.defaultdict(list)
    for w in ways:
        for i, n in enumerate(w['nodes']):
            use[tuple(n)].append((w, i))
    return use


def arms(use, node):
    """Each way end leaving the node, with the bearing of its first 15 m."""
    out = []
    for w, i in use[node]:
        for s in (-1, 1):
            j = i + s
            if not 0 <= j < len(w['nodes']):
                continue
            k = j
            while 0 < k + s < len(w['nodes']) and dist_m(w['nodes'][k], node) < 15:
                k += s
            out.append(dict(way=w, brg=bearing(node, w['nodes'][k])))
    return out


def score(use, node):
    a = arms(use, node)
    if len(a) < 3:
        return 'none', a
    has_link = any(x['way']['hw'].endswith('_link') for x in a)
    # one ref in two highway classes at this node
    byref = collections.defaultdict(set)
    for x in a:
        if x['way'].get('ref'):
            byref[x['way']['ref']].add(base_class(x['way']['hw']))
    cut = any(len(v) > 1 for v in byref.values())
    return ('ramp' if (has_link or cut) else 'strip'), a


def nearest_node(use, lat, lon, within=40):
    c = (lon, lat)
    best, bd = None, within
    for n in use:
        d = dist_m(n, c)
        if d < bd:
            best, bd = n, d
    return best, bd


def main(argv):
    use = load(argv[1])
    if len(argv) > 2:
        for spec in argv[2:]:
            lat, lon = (float(v) for v in spec.split(','))
            n, d = nearest_node(use, lat, lon)
            if n is None:
                print(f'{spec}: no node within 40 m')
                continue
            kind, a = score(use, n)
            print(f'{spec} -> node {n[1]:.5f},{n[0]:.5f} ({d:.0f} m): {kind}')
            for x in a:
                w = x['way']
                print('    %-14s ref=%-5s brg=%5.1f %s'
                      % (w['hw'], w.get('ref') or '-', x['brg'], (w.get('name') or '')[:24]))
        return 0
    counts = collections.Counter()
    for n in use:
        counts[score(use, n)[0]] += 1
    for k in ('none', 'strip', 'ramp'):
        print('%-6s %d' % (k, counts[k]))
    return 0


if __name__ == '__main__':
    import os, sys
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    import cli_help; cli_help.help_exit(__doc__)
    sys.exit(main(sys.argv))
