#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The folders a build owns on a card, read off the image, and the index checks over them."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fatread  # noqa: E402
from drstr_add import children, dir_records  # noqa: E402

SCOPE = ('MAPAL001', 'REFER001', 'REFER002', 'HOUSE001')
ROOT_FILES = ('DRSTR001.DAT', 'MMAIN001.DAT', 'CMP.LST')


def card_files(img):
    """{card path: bytes} of every file in SCOPE and the root index files."""
    out = {}
    with fatread.Fat32(img) as fs:
        for p, sz, c, _ in fs.walk():
            parts = p.strip('/').split('/')
            if sz is None:
                continue
            if (len(parts) == 2 and parts[0] in SCOPE) or (len(parts) == 1 and parts[0] in ROOT_FILES):
                data = fs.f.read_clusters(c, sz) if sz else b''
                if len(data) != sz:
                    raise SystemExit('%s on %s: %d of %d bytes in its cluster chain' % (p, img, len(data), sz))
                out['/'.join(parts)] = data
    return out


def cmp_lst(img):
    """CMP.LST from the card's own .cmp files: sorted, NUL-separated, no trailing NUL."""
    with fatread.Fat32(img) as fs:
        names = ['\\SystemSD' + p[:-4].replace('/', '\\') for p, sz, _, _ in fs.walk()
                 if sz is not None and p.endswith('.cmp')]
    return '\0'.join(sorted(names)).encode('ascii')


def drstr_check(drs, files):
    """Problems with the index's rows for the scoped folders, against the card."""
    recs = {r['name']: r for r in dir_records(drs)}
    bad = []
    for d in SCOPE:
        if d not in recs and not any(p.startswith(d + '/') for p in files):
            continue        # a QY7 card has no HOUSE001
        if d not in recs:
            bad.append('%s is on the card but not in the index' % d)
            continue
        listed = {n: sz for n, typ, sz in children(drs, recs[d]) if typ == 0}
        have = {}
        for p, b in files.items():
            if not p.startswith(d + '/'):
                continue
            n = p.split('/')[1]
            if n.endswith('.ind'):
                have[n[:-4]] = struct.unpack_from('<I', b)[0]    # indexed as one file, inflated size
            elif not n.endswith('.cmp'):
                have[n] = len(b)
        for n in sorted(set(listed) | set(have)):
            if listed.get(n) != have.get(n):
                bad.append('%s/%s index %s card %s' % (d, n, listed.get(n), have.get(n)))
    return bad
