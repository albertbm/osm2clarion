#!/usr/bin/env python3
"""Assemble a one-country card: delete every other area's files, write the built country's own.

Run on a copy of the base card, never on the base itself. Inputs are the files the
builders wrote into one directory:

    build_icode_clean.py   REFER001_ICODE001.DAT  REFER002_ICODE001.DAT
    build_addr_area0.py    ADDRE001.DAT ZCODE001.DAT POINT001.DAT INTER001.DAT
                           REFER001_COORD001.DAT  REFER002_COORD001.DAT
    lev_zero_dead.py       LEV3456R.DAT

Deleted: every address family but area 0's (ADDRE/INTER/POINT/ZCODE 002-042), every
FRWAY file (Iceland has no motorway family, the factory shape for nine countries), all
of REFER002's POINT files, all of HOUSE001 (no file there touches Iceland's box), and
ZIPFR001 unless --keep-zipfr. DRSTR001.DAT and CMP.LST are rewritten to match. Touched
directories are rewritten without stale entries and, with --zero-free, every free
cluster on the partition is zeroed so no deleted file's bytes stay on the card.

    build_clean2.py CARD.img BUILT_DIR [--keep-zipfr] [--zero-free]
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'diag'))
import fatread  # noqa: E402
from drstr_add import add as drstr_add, children, dir_records  # noqa: E402
from drstr_remove import remove as drstr_remove  # noqa: E402
from drstr_sizes import set_sizes  # noqa: E402
from fatedit import Editor  # noqa: E402
from card_pack import wrap, mmain, ed_read  # noqa: E402

FAM = re.compile(r'^(ADDRE|INTER|POINT|ZCODE|FRWAY)(\d{3})\.DAT\.(cmp|ind)$')


def deletions(img, keep_zipfr):
    fs = fatread.Fat32(img)
    out = []
    for p, sz, c, mt in fs.walk():
        if sz is None or p.count('/') != 2:
            continue
        d, n = p.strip('/').split('/')
        m = FAM.match(n)
        if d == 'REFER001' and m and (m.group(1) == 'FRWAY' or m.group(2) != '001'):
            out.append((d, n))
        elif d == 'REFER001' and n.startswith('ZIPFR001') and not keep_zipfr:
            out.append((d, n))
        elif d == 'REFER002' and n.startswith('POINT'):
            out.append((d, n))
        elif d == 'HOUSE001':
            out.append((d, n))
    fs.close()
    return out


def main(argv):
    img, built = argv[0], argv[1]
    keep_zipfr = '--keep-zipfr' in argv
    B = lambda n: open(os.path.join(built, n), 'rb').read()  # noqa: E731

    dels = deletions(img, keep_zipfr)
    ed = Editor(img)
    for d, n in dels:
        ed.delete(d, n)
    print('deleted %d files' % len(dels))

    # the built country's files
    plain = {('REFER001', 'ICODE001.DAT'): B('REFER001_ICODE001.DAT'),
             ('REFER002', 'ICODE001.DAT'): B('REFER002_ICODE001.DAT'),
             ('REFER002', 'COORD001.DAT'): B('REFER002_COORD001.DAT'),
             ('MAPAL001', 'LEV3456R.DAT'): B('LEV3456R.DAT')}
    packed = {'ADDRE001.DAT': B('ADDRE001.DAT'), 'ZCODE001.DAT': B('ZCODE001.DAT'),
              'POINT001.DAT': B('POINT001.DAT'), 'INTER001.DAT': B('INTER001.DAT'),
              'COORD001.DAT': B('REFER001_COORD001.DAT')}
    for (d, n), data in plain.items():
        ed.write(d, n, data)
    for n, data in packed.items():
        c, i = wrap(data)
        ed.write('REFER001', n + '.cmp', c)
        ed.write('REFER001', n + '.ind', i)

    # the index: drop what is gone, size what changed, add ZCODE001
    drs = ed_read(ed, '', 'DRSTR001.DAT')
    gone = {}
    for d, n in dels:
        gone.setdefault(d, set()).add(n[:-4] if n.endswith(('.cmp', '.ind')) else n)
    for d, names in gone.items():
        listed = {c[0] for c in children(drs, [r for r in dir_records(drs) if r['name'] == d][0])}
        names = sorted(names & listed)
        if names:
            drs, _, _ = drstr_remove(drs, d, names)
    drs, _, _ = drstr_add(drs, 'REFER001', [('ZCODE001.DAT', len(packed['ZCODE001.DAT']))])
    drs = set_sizes(drs, 'REFER001', {n: len(v) for n, v in packed.items() if n != 'ZCODE001.DAT'}
                    | {'ICODE001.DAT': len(plain[('REFER001', 'ICODE001.DAT')])})
    drs = set_sizes(drs, 'REFER002', {'ICODE001.DAT': len(plain[('REFER002', 'ICODE001.DAT')]),
                                      'COORD001.DAT': len(plain[('REFER002', 'COORD001.DAT')])})
    drs = set_sizes(drs, 'MAPAL001', {'LEV3456R.DAT': len(plain[('MAPAL001', 'LEV3456R.DAT')])})
    ed.write('', 'MMAIN001.DAT', mmain(ed_read(ed, '', 'MMAIN001.DAT'),
                                       [ed_read(ed, 'MAPAL001', g.name)[:0x400]
                                        for g in ed.load('MAPAL001')[1] if g.name.endswith('.DAT')]))

    lst = ed_read(ed, '', 'CMP.LST').decode('ascii').split('\0')
    gone_cmp = {'\\SystemSD\\%s\\%s' % (d, n[:-4]) for d, n in dels if n.endswith('.cmp')}
    lst = [x for x in lst if x not in gone_cmp]
    z = '\\SystemSD\\REFER001\\ZCODE001.DAT'
    if z not in lst:
        lst.append(z)
    lst.sort()
    cmplst = '\0'.join(lst).encode('ascii')
    ed.write('', 'CMP.LST', cmplst)
    drs = set_sizes(drs, '/', {'CMP.LST': len(cmplst)})
    ed.write('', 'DRSTR001.DAT', drs)

    for d in ('', 'REFER001', 'REFER002', 'HOUSE001', 'MAPAL001'):
        ed.load(d)
    if '--zero-free' in argv:
        print('zeroed %d free clusters' % ed.zero_free())
    free = ed.close()
    print('clusters zeroed by deletes and rewrites %d, free after %d' % (ed.zeroed, free))


if __name__ == '__main__':
    main(sys.argv[1:])
