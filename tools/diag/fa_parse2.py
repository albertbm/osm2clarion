import struct, sys, collections
def parse(path, verbose=False):
    p = open(path,'rb').read(); w = struct.unpack('>%dH' % (len(p)//2), p)
    HDR = {0: 7, 2: 8, 4: 9}
    i = 48; recs = []; nodes = []; bad = []
    while i < len(w):
        if w[i] in (0, 0xffff): i += 1; continue
        typ, ln = w[i] >> 12, w[i] & 0xfff
        if typ not in HDR or ln < 8 or i + ln > len(w): break
        r = w[i:i+ln]; npts = r[1] & 0xfff; j = HDR[typ]; pts = []
        for k in range(npts):
            if j + 1 >= ln: bad.append(i); break
            x, y = r[j], r[j+1]; j += 2; node = None
            if k == 0 or (y & 0x0800):
                W = None
                if y & 0x2000: W = r[j]; j += 1
                key = r[j]; j += 1; G = None
                if y & 0x1000: G = r[j]; j += 1
                seq = r[j]; j += 1; node = (W, key, G, seq)
                nodes.append(dict(rec=i, typ=typ, pos=i + j - 1, x=x & 0x7ff, y=y & 0x7ff, xf=x >> 11, yf=y >> 11, W=W, key=key, G=G, seq=seq, k=k))
            pts.append((x, y, node))
        tail = tuple(r[j:])
        recs.append(dict(at=i, typ=typ, ln=ln, npts=npts, hdr=tuple(r[2:HDR[typ]]), pts=pts, tail=tail))
        if verbose:
            print('w%4d typ%d len%2d npts%2d hdr %s tail %s' % (i, typ, ln, npts, ' '.join('%04x' % x for x in r[2:HDR[typ]]), ' '.join('%04x' % x for x in tail)))
            print('     ', ' | '.join('(%d,%d x%x y%x)%s' % (x & 0x7ff, y & 0x7ff, x >> 11, y >> 11, '' if n is None else ' W=%s key=%04x G=%s seq=%04x' % ('%04x' % n[0] if n[0] is not None else '-', n[1], n[2], n[3])) for x, y, n in pts))
        i += ln
    return w, recs, nodes, bad, i
if __name__ == '__main__':
    w, recs, nodes, bad, end = parse(sys.argv[1], len(sys.argv) > 2)
    print('records', len(recs), 'types', collections.Counter(r['typ'] for r in recs), 'bad', bad, 'end w%d of %d' % (end, len(w)))
    print('tail lens', collections.Counter(len(r['tail']) for r in recs))
    print('nodes with G:')
    for n in nodes:
        if n['G'] is not None: print('   rec w%d typ%d node w%d (%d,%d) W=%s key=%04x G=%d seq=%04x' % (n['rec'], n['typ'], n['pos'], n['x'], n['y'], '%04x' % n['W'] if n['W'] is not None else '-', n['key'], n['G'], n['seq']))
    keys = sorted(set('%04x' % n['key'] for n in nodes if n['key'] != 0xffff)); tails = sorted(set('%04x' % t[0] for r in recs for t in [r['tail']] if t and t[0] != 0xffff))
    print('node keys:', ' '.join(keys)); print('tails:', ' '.join(tails))
    print('after units:', ' '.join('%04x' % x for x in w[end:end+24]))
