#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a finished card as a whole: what card_check's road rules do not look at.

    card_verify.py --card IMG --version VV/VV/VV/VV [--towns] [--manifest M.json]
                   [--allow-new PATTERN ...] [--skip NAME ...]

version   every DATA VERSION stamp in MAPAL001, REFER001, REFER002 and HOUSE001 is the unit's,
          and every map tile and HOUSE001 file carries one
index     DRSTR001.DAT's rows for those folders match the files on the card
cmp_lst   CMP.LST lists exactly the card's .cmp files
mmain     MMAIN001.DAT's level counts and db box are the sums of the map file headers
towns     (--towns) every ADDRE street name's town index points at the card ICODE row of the
          town the address register gives that street; needs $O2C_CONFIG for the register
inventory (--manifest) every file the cards step recorded is still on the card; files it did
          not record fail unless --allow-new matches them. The final per-file hashes are
          written to the manifest as `final_files`.

--skip leaves checks out: area mode skips cmp_lst, since it keeps the stock card's CMP.LST.

Exit status 0 only if every check passes.
"""
import argparse
import collections
import fnmatch
import hashlib
import json
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'addr'))
import fatread  # noqa: E402
from card_pack import mmain_card, stamp_text  # noqa: E402
from card_scope import cmp_lst, drstr_check, entries  # noqa: E402

UNREGISTERED = 0.01     # the share of named records whose street the register may lack
STAMP = re.compile(re.escape(stamp_text('')) + b'((?:\x00[0-9/]){11})')
STAMP_LEN = len(stamp_text('')) + 22
# read whole: the root index files, the .ind halves and what the towns check parses
WHOLE = ('REFER001/ADDRE001.DAT.cmp', 'REFER001/ADDRE001.DAT', 'REFER001/ICODE001.DAT')


def result(name, ok, counts, notes=()):
    return dict(name=name, passed=bool(ok), counts=dict(counts), notes=list(notes))


class Stamps:
    """DATA VERSION stamps in a byte stream fed piece by piece."""

    def __init__(self):
        self.found, self.tail = set(), b''

    def feed(self, b):
        buf = self.tail + b
        self.found |= {m.group(1).decode('utf-16-be') for m in STAMP.finditer(buf)}
        self.tail = buf[-(STAMP_LEN - 1):]


def stamps_in(b):
    st = Stamps()
    st.feed(b)
    return st.found


def read_card(img):
    """One streamed pass over the scoped files, so a full stock card fits in memory.
    -> ({path: bytes for the few read whole, else the size}, {path: dict(size, sha256,
    stamps, head)}, {path: plain bytes of the files read whole})."""
    files, info, plain = {}, {}, {}
    with fatread.Fat32(img) as fs:
        ents = entries(fs)
        for p, (sz, c) in ents.items():
            if p.endswith('.ind'):
                files[p] = b''.join(fs.chunks(c, sz))
        for p, (sz, c) in sorted(ents.items()):
            whole = '/' not in p or p in WHOLE
            h, st, head, keep, out = hashlib.sha256(), Stamps(), b'', [], []
            ind = files.get(p[:-4] + '.ind') if p.endswith('.cmp') else None
            if p.endswith('.cmp') and ind is None:
                raise SystemExit('%s has no .ind on the card' % p)
            if ind is not None:
                n, _, blocks, _ = struct.unpack_from('<IIII', ind)
                offs = list(struct.unpack_from('<%dI' % blocks, ind, 16)) + [sz]
                buf, pos, k = b'', 0, 0
            for d in fs.chunks(c, sz):
                h.update(d)
                if len(head) < 0x400:
                    head += d[:0x400 - len(head)]
                if whole:
                    keep.append(d)
                if ind is None:
                    if '/' in p and not p.endswith('.ind'):
                        st.feed(d)
                    continue
                # a packed file's blocks are separate zlib streams; scan each one unpacked
                buf += d
                while k < blocks and pos + len(buf) >= offs[k + 1]:
                    pl = zlib.decompress(buf[offs[k] - pos:offs[k + 1] - pos])
                    buf, pos, k = buf[offs[k + 1] - pos:], offs[k + 1], k + 1
                    st.feed(pl)
                    if whole:
                        out.append(pl)
            if ind is not None and k != blocks:
                raise SystemExit('%s: %d of %d packed blocks on the card' % (p, k, blocks))
            if whole:
                files[p] = b''.join(keep)
                if ind is not None:
                    plain[p] = b''.join(out)
                    if len(plain[p]) != n:
                        raise SystemExit('%s unpacks to %d bytes, its .ind says %d' % (p, len(plain[p]), n))
                elif '/' in p:
                    plain[p] = files[p]
            elif p not in files:
                files[p] = sz
            info[p] = dict(size=sz, sha256=h.hexdigest(), stamps=st.found, head=head)
    return files, info, plain


def check_version(stamps, version):
    """stamps: {path: the stamps found in it, or its plain bytes}."""
    st, notes = collections.Counter(), []
    for p, b in sorted(stamps.items()):
        found = b if isinstance(b, set) else stamps_in(b)
        st['stamped' if found else 'no stamp'] += 1
        if found - {version}:
            notes.append('%s carries %s' % (p, ', '.join(sorted(found))))
        if not found and p.startswith(('MAPAL001/', 'HOUSE001/')) and p.endswith('.DAT'):
            notes.append('%s carries no DATA VERSION' % p)
    if not st['stamped']:
        notes.append('no file carries a DATA VERSION')
    return result('version', not notes, st, notes)


def check_index(files):
    bad = drstr_check(files['DRSTR001.DAT'], files)
    return result('index', not bad, dict(rows_off=len(bad)), bad[:20])


def check_cmp_lst(img, files):
    want = cmp_lst(img)
    ok = files.get('CMP.LST') == want
    return result('cmp_lst', ok, dict(entries=want.count(b'\0') + 1 if want else 0),
                  [] if ok else ['CMP.LST does not list the card\'s .cmp files'])


def check_mmain(files, info):
    heads = [info[p]['head'] for p in sorted(info) if p.startswith('MAPAL001/') and p.endswith('.DAT')]
    nhouse = sum(1 for p in files if p.startswith('HOUSE001/') and p.endswith('.DAT'))
    have = files['MMAIN001.DAT']
    try:
        want = mmain_card(have, heads, nhouse)
    except ValueError as e:
        return result('mmain', False, {}, [str(e)])
    diff = [i for i in range(min(len(have), len(want))) if have[i] != want[i]]
    return result('mmain', not diff and len(have) == len(want), dict(map_files=len(heads), house=nhouse),
                  ['%d bytes differ from the header sums, first at 0x%x' % (len(diff), diff[0])] if diff else [])


def icode_town_rows(ic):
    """The town rows of ICODE's first area frame."""
    import icode_io
    k = 0
    while True:
        rec = struct.unpack_from('>HIIIHHBBHHH', ic, 0x5c00 + 26 * k)
        if rec[7] == 1:
            break
        k += 1
    o = (rec[1] >> 8) * 2048 + (rec[1] & 0x3f) * 256
    frame = ic[o:o + rec[2] * 2]
    top = icode_io.read_list(frame, 0)
    return [t.name.decode() for t in icode_io.read_list(frame, top[0].roff * 2)]


def register_pairs():
    """{(STREET, TOWN)} of the register's addresses in the config's area municipalities."""
    import addr_csv
    cfg = addr_csv._config()['addr']
    svf = {k for a in cfg['area_municipalities'].values() for k in a}
    tn = addr_csv.towns()
    return {(r['street'].upper(), tn[r['post']].upper()) for r in addr_csv.addresses(municipalities=svf)
            if r['post'] in tn}


def check_towns(plain, pairs):
    import addre_io
    ad, ic = plain.get('REFER001/ADDRE001.DAT.cmp') or plain.get('REFER001/ADDRE001.DAT'), \
        plain.get('REFER001/ICODE001.DAT')
    if ad is None or ic is None:
        return result('towns', False, {}, ['no ADDRE or ICODE on the card'])
    rows = icode_town_rows(ic)
    known = {s for s, _ in pairs}
    st = collections.Counter(icode_rows=len(rows))
    bad = collections.Counter()
    for r in addre_io.Addre(ad).names:
        p = addre_io.parse_name_record(r)
        if p['name'] is None:
            continue
        t, street = p['levels'][1], p['name']
        town = rows[t] if t < len(rows) else None
        st['named'] += 1
        if town is None:
            bad['index past the ICODE rows'] += 1
        elif (street, town) in pairs:
            st['match'] += 1
        elif street in known:
            bad['%2d %s' % (t, town)] += 1
        else:
            st['not in register'] += 1
    st['mismatch'] = sum(bad.values())
    missing = sorted({t for _, t in pairs} - set(rows))
    return result('towns', *towns_verdict(st, bad, missing))


def towns_verdict(st, bad, missing):
    """(passed, counts, notes) of the towns check from its counts."""
    notes = ['%d named records at row %s' % (n, k) for k, n in bad.most_common(10)]
    if missing:
        notes.append('register towns with no ICODE row: ' + ', '.join(missing[:10]))
    # an empty or stale register puts every record here and leaves nothing to compare
    if not st['match']:
        notes.append('no named record matches the register')
    if st['not in register'] > UNREGISTERED * st['named']:
        notes.append('%d of %d named records have a street not in the register'
                     % (st['not in register'], st['named']))
    return not notes and st['named'] > 0, st, notes


def check_inventory(files, manifest, allow):
    # the root index files are rewritten by every step, so only the folders are inventoried
    before = {r['path'] for r in manifest.get('files', []) if '/' in r['path']}
    have = {p for p in files if '/' in p}
    gone = sorted(before - have)
    new = sorted(p for p in have - before if not any(fnmatch.fnmatch(p, a) for a in allow))
    notes = ['gone since the cards step: ' + p for p in gone] + ['not made by the cards step: ' + p for p in new]
    return result('inventory', not notes, dict(files=len(files), gone=len(gone), new=len(new)), notes[:20])


def verify(img, version, towns=False, manifest=None, allow=(), skip=()):
    files, info, plain = read_card(img)
    stamps = {p: i['stamps'] for p, i in info.items() if '/' in p and not p.endswith('.ind')}
    checks = [c() for name, c in (('version', lambda: check_version(stamps, version)),
                                  ('index', lambda: check_index(files)),
                                  ('cmp_lst', lambda: check_cmp_lst(img, files)),
                                  ('mmain', lambda: check_mmain(files, info))) if name not in skip]
    if towns:
        checks.append(check_towns(plain, register_pairs()))
    if manifest is not None:
        checks.append(check_inventory(files, manifest, allow))
    return checks, info


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--card', required=True)
    ap.add_argument('--version', required=True)
    ap.add_argument('--towns', action='store_true')
    ap.add_argument('--manifest')
    ap.add_argument('--allow-new', nargs='*', default=[])
    ap.add_argument('--skip', nargs='*', default=[], choices=['version', 'index', 'cmp_lst', 'mmain'])
    a = ap.parse_args(argv)
    m = None
    if a.manifest:
        with open(a.manifest) as f:
            m = json.load(f)
    checks, info = verify(a.card, a.version, a.towns, m, a.allow_new, a.skip)
    print('== %s ==' % os.path.basename(a.card))
    for c in checks:
        print('[%s] %s  %s' % ('PASS' if c['passed'] else 'FAIL', c['name'],
                               ' '.join('%s=%s' % kv for kv in c['counts'].items())))
        for n in c['notes']:
            print('    ' + n)
    ok = all(c['passed'] for c in checks)
    if m is not None:
        m['final_files'] = [dict(path=p, size=i['size'], sha256=i['sha256']) for p, i in sorted(info.items())]
        m['final_checks'] = checks
        with open(a.manifest, 'w') as f:
            json.dump(m, f, indent=1, ensure_ascii=False)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
