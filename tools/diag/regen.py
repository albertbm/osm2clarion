#!/usr/bin/env python3
"""Run make_drstr.build against a card image instead of a mounted card.

os.scandir and pathlib.Path inside make_drstr are redirected at the image's FAT
directories, in on-disk order, so make_drstr's own listing(), scan(), tree_size()
and build() run unchanged. The output is compared with the card's own
DRSTR001.DAT. Reads only the sectors the FAT walk needs.
"""
import os, posixpath, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from fatread import Fat
import make_drstr

BYTES = [0]
_rd = Fat.rd
def rd(self, off, n):
    BYTES[0] += n
    return _rd(self, off, n)
Fat.rd = rd


def readdir(f, clus):
    if clus is None and f.fat32:
        clus = f.rootclus
    data = f.read_clusters(clus)
    out, lfn = [], {}
    for i in range(0, len(data), 32):
        e = data[i:i+32]
        if len(e) < 32 or e[0] == 0:
            break
        if e[0] == 0xe5:
            lfn = {}; continue
        if e[11] == 0x0f:
            lfn[e[0] & 0x3f] = (e[1:11] + e[14:26] + e[28:32]).decode(
                'utf-16-le', 'replace').split('￿')[0].split('\x00')[0]
            continue
        if e[11] & 0x08 and not e[11] & 0x10:
            lfn = {}; continue
        if lfn:
            full = ''.join(lfn[k] for k in sorted(lfn))
        else:
            stem = e[0:8].decode('latin1').rstrip()
            ext = e[8:11].decode('latin1').rstrip()
            full = stem + ('.' + ext if ext else '')
        lfn = {}
        if full in ('.', '..'):
            continue
        out.append((full, bool(e[11] & 0x10),
                    struct.unpack('<H', e[20:22])[0] << 16 | struct.unpack('<H', e[26:28])[0],
                    struct.unpack('<I', e[28:32])[0]))
    return out


class Card:
    def __init__(self, path):
        self.f = Fat(path)
        self.f.open_part(self.f.parts[0][2])
        self.tree = {}
        self.ent = {}
        self._walk('/', None)

    def _walk(self, path, clus):
        ents = readdir(self.f, clus)
        self.tree[path] = ents
        for name, isdir, c, sz in ents:
            p = posixpath.join(path, name)
            self.ent[p] = (isdir, c, sz)
            if isdir and not name.startswith('.') and name != 'System Volume Information':
                self._walk(p, c)

    def head(self, path, n):
        isdir, c, sz = self.ent[path]
        return self.f.rd(self.f.csec(c) * self.f.bps, n)

    def read(self, path):
        isdir, c, sz = self.ent[path]
        return self.f.read_clusters(c, sz)


class IPath:
    """Enough of pathlib.Path for make_drstr: joining, str(), read_bytes()."""
    def __init__(self, card, p):
        self.card, self.p = card, str(p)
    def __truediv__(self, other):
        return IPath(self.card, posixpath.join(self.p, str(other)))
    def __str__(self):
        return self.p
    def read_bytes(self):
        return self.card.head(self.p, 512)


class IEntry:
    def __init__(self, card, parent, row):
        self.card = card
        self.name, self._isdir, self._clus, self._size = row
        self.path = posixpath.join(parent, self.name)
    def is_dir(self):
        return self._isdir
    def stat(self):
        return self
    @property
    def st_size(self):
        return self._size


def check(img):
    card = Card(img)
    root = card.tree['/']
    drstr = [e for e in root if e[0].upper() == 'DRSTR001.DAT']
    print('%s' % os.path.basename(img))
    if not drstr:
        print('  no DRSTR001.DAT'); return
    own = card.read('/DRSTR001.DAT')

    def scandir(path):
        p = str(path)
        return [IEntry(card, p, r) for r in card.tree[p]
                if not r[0].startswith('.') and r[0] != 'System Volume Information']

    make_drstr.os.scandir = scandir
    make_drstr.Path = lambda p: p if isinstance(p, IPath) else IPath(card, p)
    built = make_drstr.build(IPath(card, '/'))

    print('  card DRSTR001.DAT %d bytes, regenerated %d bytes' % (len(own), len(built)))
    if built == own:
        print('  IDENTICAL, byte for byte')
    else:
        n = min(len(own), len(built))
        d = [i for i in range(n) if own[i] != built[i]]
        print('  DIFFERS at %d of %d common bytes, first at 0x%x' % (len(d), n, d[0] if d else n))
        for i in d[:4]:
            rec = i // 0x28
            print('     0x%05x card %s built %s' % (i, own[i:i+8].hex(), built[i:i+8].hex()))
    print('  bytes read from the image: %d' % BYTES[0])
    print()


for a in sys.argv[1:]:
    BYTES[0] = 0
    check(a)
