#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare a card's FAT readdir order against the order in its own DRSTR001.DAT.

Reads only the MBR, the FAT32 boot sector, the FAT sectors it needs for the
directory chains, the directory clusters themselves, and DRSTR001.DAT.
Built on fatread.py; adds long-name assembly, which the index needs
because names like ADDRE001.DAT.cmp do not fit an 8.3 short name.
"""
import os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fatread import Fat

BYTES_READ = [0]
_orig = Fat.rd
def rd(self, off, n):
    BYTES_READ[0] += n
    return _orig(self, off, n)
Fat.rd = rd


def readdir_lfn(f, clus):
    """Entries in on-disk order, long names assembled, . and .. and deleted dropped."""
    if clus is None and f.fat32:
        clus = f.rootclus
    data = f.read_clusters(clus) if clus else f.rd(f.rootdirsec*f.bps, f.rootents*32)
    ents = []
    lfn = {}
    for i in range(0, len(data), 32):
        e = data[i:i+32]
        if len(e) < 32 or e[0] == 0:
            break
        if e[0] == 0xe5:
            lfn = {}
            continue
        if e[11] == 0x0f:
            seq = e[0] & 0x3f
            chars = e[1:11] + e[14:26] + e[28:32]
            s = chars.decode('utf-16-le', 'replace')
            lfn[seq] = s.split('￿')[0].split('\x00')[0]
            continue
        if e[11] & 0x08 and not e[11] & 0x10:
            lfn = {}
            continue            # volume label
        if lfn:
            full = ''.join(lfn[k] for k in sorted(lfn))
        else:
            name = e[0:8].decode('latin1').rstrip()
            ext = e[8:11].decode('latin1').rstrip()
            full = name + ('.' + ext if ext else '')
        lfn = {}
        if full in ('.', '..') or full.startswith('.'):
            continue
        c = struct.unpack('<H', e[20:22])[0] << 16 | struct.unpack('<H', e[26:28])[0]
        sz = struct.unpack('<I', e[28:32])[0]
        ents.append((full, bool(e[11] & 0x10), c, sz))
    return ents


def fat_tree(f):
    """{index-style path: [child names in on-disk order]} by walking the FAT."""
    out = {}
    queue = [('/', None)]
    while queue:
        path, clus = queue.pop(0)
        ents = readdir_lfn(f, clus)
        out[path] = [n for n, _, _, _ in ents]
        for n, isdir, c, _ in ents:
            if isdir and n != 'System Volume Information' and not n.startswith('.'):
                queue.append((os.path.join(path, n), c))
    return out


def index_order(d):
    """{path: [child names in index order]} from a DRSTR001.DAT image."""
    recs = []
    i = 0
    while True:
        name, own, parent, cnt = struct.unpack_from('<20sHHH', d, i)
        off = struct.unpack_from('<I', d, i + 0x24)[0]
        recs.append((name.rstrip(b'\0').decode(), own, parent, cnt, off))
        i += 0x28
        if i >= recs[0][4]:
            break
    paths = {}
    for name, own, parent, cnt, off in recs:
        paths[own] = name if own == parent else os.path.join(paths[parent], name)
    order = {}
    for name, own, parent, cnt, off in recs:
        order[paths[own]] = [d[off + k*0x20:off + k*0x20 + 20].rstrip(b'\0').decode()
                             for k in range(cnt)]
    return order


def main():
    img = sys.argv[1]
    f = Fat(img)
    lba = f.parts[0][2]
    f.open_part(lba)
    print('%s' % os.path.basename(img))
    print('  partition 1 at LBA %d, label %r, fstype %r, cluster %d bytes'
          % (lba, f.label.strip(), f.fstype.strip(), f.spc * f.bps))
    root = readdir_lfn(f, None)
    drstr = [e for e in root if e[0].upper() == 'DRSTR001.DAT']
    if not drstr:
        print('  no DRSTR001.DAT'); return
    _, _, c, sz = drstr[0]
    d = f.read_clusters(c, sz)
    print('  DRSTR001.DAT %d bytes' % sz)
    idx = index_order(d)
    fat = fat_tree(f)

    # The index omits SPEECH and DRSTR001.DAT, and lists a .cmp/.ind pair as one name.
    def normalise(names):
        out = []
        for n in names:
            if n in ('SPEECH', 'DRSTR001.DAT', 'System Volume Information') or n.endswith('.ind'):
                continue
            out.append(n[:-4] if n.endswith('.cmp') else n)
        return out

    agree = disagree = missing = 0
    for path in sorted(idx):
        want = idx[path]
        if path not in fat:
            print('  MISSING on disk: %s' % path); missing += 1; continue
        got = normalise(fat[path])
        if got == want:
            agree += 1
        else:
            disagree += 1
            print('  DIFFERS %s (%d entries)' % (path, len(want)))
            print('     index  : %s' % ', '.join(want[:8]))
            print('     readdir: %s' % ', '.join(got[:8]))
            if sorted(got) != sorted(want):
                print('     name sets differ: only in index %s / only on disk %s'
                      % (sorted(set(want) - set(got))[:5], sorted(set(got) - set(want))[:5]))
            else:
                perm = [k for k, (a, b) in enumerate(zip(got, want)) if a != b]
                print('     same names, %d of %d positions differ, first at %d'
                      % (len(perm), len(want), perm[0]))
    print('  directories: %d order-identical, %d differing, %d absent on disk'
          % (agree, disagree, missing))
    print('  bytes read from the image: %d' % BYTES_READ[0])
    print()


main()
