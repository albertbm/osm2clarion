import sys, collections, osmium
class H(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.kind=collections.Counter(); self.shape=collections.Counter()
        self.roles=collections.Counter(); self.n=0; self.samples=[]
    def relation(self, r):
        t=r.tags.get('type')
        if t not in ('restriction',) and not (t or '').startswith('restriction'): return
        self.n+=1
        val=r.tags.get('restriction') or r.tags.get('restriction:motorcar') or r.tags.get('restriction:hgv') or '(none)'
        self.kind[val]+=1
        ms=[(m.type, m.role, m.ref) for m in r.members]
        self.roles[tuple(sorted((m.type,m.role) for m in r.members))]+=1
        via=[m for m in r.members if m.role=='via']
        self.shape[(len(via), via[0].type if via else None)]+=1
        if len(self.samples)<6: self.samples.append((r.id, t, val, ms))
h=H(); h.apply_file(sys.argv[1])
print('restriction relations:', h.n)
print('restriction values:', h.kind.most_common())
print('via shape (count, type):', dict(h.shape))
print('member role signatures:')
for k,v in h.roles.most_common(8): print('   %4d  %s' % (v,k))
for s in h.samples: print('  sample', s)
