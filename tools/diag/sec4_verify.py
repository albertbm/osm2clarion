#!/usr/bin/env python3
"""Check that adding a section 4 changed nothing else in the tile.

Pairs every section-1 and section-2 record of the old tile with the new one by
(section, level, directory y/x, slot, pair index) and compares the compressed bytes, so
a changed road shape or background cannot hide behind a different record order. Then
diffs the 0x1000-byte header byte by byte and names which bytes moved.

    python3 tools/sec4_verify.py old.DAT new.DAT
"""
import os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import clarion_tile


def index(d):
    t = clarion_tile.read_tile(d)
    out = {}
    for sec in t['sections']:
        if sec['id'] == 4:
            continue
        for L in sec['levels']:
            for D in L['dirs']:
                for slot, s in D['slots'].items():
                    for r in s['recs']:
                        out[(sec['id'], L['level'], D['y'], D['x'], slot, r['k'])] = bytes(r['raw'])
    return t, out


def main(old, new):
    a, ia = index(open(old, 'rb').read())
    b, ib = index(open(new, 'rb').read())
    only_a, only_b = set(ia) - set(ib), set(ib) - set(ia)
    diff = [k for k in set(ia) & set(ib) if ia[k] != ib[k]]
    print('%s -> %s' % (old.split('/')[-1], new.split('/')[-1]))
    print('  sections 1+2 records: old %d  new %d  paired %d  only in old %d  only in new %d  differing %d'
          % (len(ia), len(ib), len(set(ia) & set(ib)), len(only_a), len(only_b), len(diff)))
    for k in sorted(diff)[:5]:
        print('    differs at %s' % (k,))
    ha, hb = bytes(a['header']), bytes(b['header'])
    runs, i = [], 0
    while i < len(ha):
        if ha[i] != hb[i]:
            j = i
            while j < len(ha) and ha[j] != hb[j]:
                j += 1
            runs.append((i, ha[i:j], hb[i:j]))
            i = j
        else:
            i += 1
    print('  header: %d differing runs' % len(runs))
    for off, x, y in runs:
        print('    %#06x  old %s  new %s' % (off, x.hex(' '), y.hex(' ')))
    sa = [(s['id'], s['count']) for s in a['sections']]
    sb = [(s['id'], s['count']) for s in b['sections']]
    print('  section table: old %s  new %s' % (sa, sb))
    return 0 if not (only_a or only_b or diff) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
