#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-tile record tally by (section, level), plus level-2 graph node/link totals."""
import collections, os, resource, struct, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan
import clarion_rd

for path in sys.argv[1:]:
    with TileScan(path) as t:
        tally = collections.Counter(); bad = []
        nodes = links = 0; l2recs = 0; kinds = collections.Counter()
        for r in t.records():
            tally[(r.section, r.level)] += 1
            try:
                p = r.plain()
            except Exception as e:
                bad.append((r.section, r.level, r.slot, r.k, 'inflate failed: %s' % e)); continue
            if r.is_zlib and len(p) // 2 != r.words:
                bad.append((r.section, r.level, r.slot, r.k, 'words %d != %d' % (len(p)//2, r.words)))
            if r.section == 2 and r.level == 2:
                l2recs += 1
                kinds[struct.unpack_from('>H', p, 52)[0]] += 1
                rec = clarion_rd.parse_record(p)
                if 'error' in rec: bad.append((2, 2, r.slot, r.k, rec['error']))
                nodes += rec.get('nnodes', 0); links += rec.get('nlinks', 0)
        print('%-34s %12d B' % (os.path.basename(path), os.path.getsize(path)))
        for k in sorted(tally): print('    sec %s lev %s  %7d records' % (k[0], k[1], tally[k]))
        print('    level-2 graph records: %d   nodes %d   links %d   header tails %s'
              % (l2recs, nodes, links, dict(kinds)))
        print('    malformed records: %d%s' % (len(bad), '  ' + str(bad[:5]) if bad else ''))
print('peak RSS %.2f GB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9))
