#!/usr/bin/env python3
"""Road data tally of a tile: level-1 shape distribution, graph sizes, regulation records,
implied speeds. The before/after check for a writer change.

    roadstats.py TILE...
"""
import collections, os, statistics, struct, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import clarion_rd
from tile_scan import TileScan


def parse_shape(w, p, hdr2=2):
    """(points, class, links) of the level-1 shape at word p, or None when it does not close."""
    ln = w[p] & 0x1fff; ex = 1 if w[p] & 0x2000 else 0
    if ln < 10 or p + ln > len(w): return None
    npts = w[p + 1] & 0x3fff; cls = w[p + 2] >> 12
    pts = [(w[p + 7 + ex] & 0x7ff, w[p + 8 + ex] & 0x7ff)]
    q = p + 9 + ex; end = p + ln; nlinks = 0
    while len(pts) < npts:
        h = 3 if nlinks == 0 else hdr2
        if q + h > end: return None
        q += h; nlinks += 1
        while True:
            if q + 2 > end: return None
            x, y = w[q], w[q + 1]; q += 2
            pts.append((x & 0x7ff, y & 0x7ff))
            if len(pts) == npts or y & 0x0800: break
    if q + 1 != end: return None
    return pts, cls, nlinks


def shapes_of(pl):
    w = struct.unpack_from('>%dH' % (len(pl) // 2), pl)
    if w[2] != 0x00fa or w[7] != 11: return [], 0
    out = []
    for lay in range(12):
        off, cnt = w[23 + 2 * lay], w[24 + 2 * lay]
        if off == 0xffff: continue
        q = 22 + 2 * off
        for _ in range(cnt):
            if q + 9 > len(w) or w[q] == 0: break
            res = parse_shape(w, q) or parse_shape(w, q, 3)
            out.append((lay, res)); q += w[q] & 0x1fff
    return out, w[8]   # w8: geometry unit length in dwords


def speeds(rec):
    for nd in rec['nodes']:
        for e in nd['entries']:
            if e['cls'] & 0x8000: continue
            a, b = e['a'], e['b']
            secs = (a & 0xfff) * 4 ** ((a >> 12) & 7) * 0.1; L = (b & 0xfff) * 4 ** ((b >> 12) & 7)
            if secs > 0 and L > 0: yield L / secs * 3.6


def run(path):
    pts = collections.Counter(); links = collections.Counter(); percls = collections.Counter()
    nrec = unparsed = 0; maxunit = maxlayer = 0
    clspts = collections.defaultdict(list); clslinks = collections.defaultdict(list)
    g = {1: dict(recs=0, nodes=0, ends=0, edges=0, regs=0, kmh=[]), 2: dict(recs=0, nodes=0, ends=0, edges=0, regs=0, kmh=[])}
    with TileScan(path) as t:
        for r in t.records(section=1, level=1, even_only=True):
            pl = r.plain()
            if len(pl) < 6 or struct.unpack_from('>H', pl, 4)[0] != 0x00fa: continue
            nrec += 1; shapes, unit = shapes_of(pl); maxunit = max(maxunit, 2 * unit)
            maxlayer = max(maxlayer, max(collections.Counter(lay for lay, _ in shapes).values(), default=0))
            for lay, res in shapes:
                if res is None: unparsed += 1; continue
                p, cls, nl = res
                pts[len(p)] += 1; links[nl] += 1; percls[(lay, cls)] += 1
                clspts[cls].append(len(p)); clslinks[cls].append(nl)
        for lev in (1, 2):
            for r in t.records(section=2, level=lev):
                rec = clarion_rd.parse_record(r.plain()); G = g[lev]; G['recs'] += 1
                G['nodes'] += len(rec['nodes'])
                for nd in rec['nodes']:
                    for e in nd['entries']:
                        if e['cls'] & 0x8000: G['edges'] += 1
                        else: G['ends'] += 1
                        G['regs'] += len(e['extra'])
                G['kmh'] += list(speeds(rec))
    tot = sum(pts.values()); ptsl = sorted(pts.elements())
    print('%s' % path.split('/')[-1])
    if not tot: print('  no level-1 shapes'); return tot, pts, links
    print('  level-1 shapes %d in %d records, %d unparsed; points/shape median %d mean %.2f max %d; 2-point %.1f%%; multi-link %.1f%% (links/shape mean %.2f max %d); largest geometry unit %d words'
          % (tot, nrec, unparsed, statistics.median(ptsl), sum(ptsl) / tot, max(ptsl), 100 * pts[2] / tot,
             100 * (tot - links[1]) / tot, sum(links.elements()) / tot, max(links), maxunit))
    print('  level-1 shapes by (layer, class): %s; most shapes in one layer of one record %d' % (', '.join('%d/%x %d' % (l, c, n) for (l, c), n in sorted(percls.items())), maxlayer))
    for cls in sorted(clspts):
        cp, cl = sorted(clspts[cls]), clslinks[cls]
        print('    class %x: %6d shapes, points median %d mean %.2f max %d, 2-point %.1f%%, multi-link %.1f%%, links/shape mean %.2f'
              % (cls, len(cp), statistics.median(cp), sum(cp) / len(cp), cp[-1], 100 * cp.count(2) / len(cp), 100 * sum(1 for n in cl if n > 1) / len(cl), sum(cl) / len(cl)))
    for lev in (1, 2):
        G = g[lev]
        if not G['recs']: continue
        print('  level-%d graph: %d records, %d nodes, %d link-ends (%d links), %d edge connections, %d regulation words, speed median %.1f km/h over %d link-ends'
              % (lev, G['recs'], G['nodes'], G['ends'], G['ends'] // 2, G['edges'], G['regs'], statistics.median(G['kmh']), len(G['kmh'])))
    return tot, pts, links


if __name__ == '__main__':
    allp = collections.Counter(); alll = collections.Counter()
    for p in sys.argv[1:]:
        tot, pts, links = run(p); allp.update(pts); alll.update(links)
    if len(sys.argv) > 2:
        tot = sum(allp.values()); el = sorted(allp.elements())
        print('ALL: %d shapes, points/shape median %d mean %.2f max %d, 2-point %.1f%%, multi-link %.1f%%, links/shape mean %.2f'
              % (tot, statistics.median(el), sum(el) / tot, max(el), 100 * allp[2] / tot, 100 * (tot - alll[1]) / tot, sum(alll.elements()) / tot))
