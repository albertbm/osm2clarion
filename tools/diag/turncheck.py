#!/usr/bin/env python3
"""Read the regulation records back out of built tiles and check them."""
import os, sys, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from tile_scan import TileScan
import clarion_rd

FACTORY = {0xf0ff,0x10ff,0x20ff,0x00ff,0x30ff,0x1001,0xf001,0x2001,0x0001,0x1002,0x0002,0x2002}
tot=collections.Counter(); grand=0; bad=[]; percount=collections.Counter(); nodes_with=0
for path in sys.argv[1:]:
    n=0; vals=collections.Counter(); perentry=collections.Counter()
    with TileScan(path) as t:
        for r in t.records(section=2, level=1):
            rec=clarion_rd.parse_record(r.plain())
            if 'error' in rec: bad.append((path, rec['error']))
            for nd in rec['nodes']:
                if any(e['extra'] for e in nd['entries']): nodes_with+=1
                for k,e in enumerate(nd['entries']):
                    ex=e['extra']
                    if not ex: continue
                    # the count field must equal the number of words that follow b
                    if ((e['w5']>>6)&0xf) != len(ex): bad.append((path,'count field %d != %d words'%((e['w5']>>6)&0xf,len(ex))))
                    if e['w5'] & ~0x03c0: bad.append((path,'w5 has bits outside the count: %#x'%e['w5']))
                    perentry[len(ex)]+=1
                    for x in ex:
                        vals[x]+=1; n+=1
                        en, ex_nib = (x>>12)&0xf, (x>>8)&0xf
                        if ex_nib != 0: bad.append((path,'exit nibble %#x not 0'%ex_nib))
                        if (x & 0xff) != 0xff: bad.append((path,'low byte %#x not 0xff'%(x&0xff)))
                        if en > 14 and en != 15: bad.append((path,'entry nibble %d'%en))
    print('%-14s %5d regulation records   values %s' % (path.split('/')[-1], n,
          sorted('%04x'%v for v in vals)))
    tot.update(vals); grand+=n; percount.update(perentry)
print('total %d records across %d tiles' % (grand, len(sys.argv)-1))
print('distinct values written: %s' % [('%04x'%v, tot[v]) for v in sorted(tot)])
outside=[('%04x'%v) for v in tot if v not in FACTORY]
print('values NOT in the factory set of twelve: %s' % (outside or 'none'))
print('records per entry: %s' % dict(sorted(percount.items())))
print('nodes carrying at least one record: %d' % nodes_with)
print('problems: %d%s' % (len(bad), '  ' + str(bad[:5]) if bad else ''))
