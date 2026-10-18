#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Delete, replace and create files by long name in a FAT32 card image, and leave no trace.

`fatwrite.py` frees a deleted file's clusters but leaves its bytes on the card and its
directory entry marked 0xe5, and it only matches 8.3 names. A clean card wants neither:
another country's data must not survive in free space or in a stale entry. So here

- a deleted or replaced file's old clusters are zeroed, not just freed;
- a touched directory is rewritten from its live entries, with every deleted entry and
  orphaned long-name slot dropped and the rest of its cluster chain zeroed;
- a new file gets VFAT long-name entries and a generated 8.3 alias, so `ZCODE001.DAT.cmp`
  can be created the way the factory names it.

    fatedit.py IMAGE --write SPEC...
      DIR:-NAME           delete
      DIR:NAME=SRC        replace or create
      --zero-free         zero every free cluster in the partition at the end
      --compact DIR       rewrite a directory even if nothing in it changed

DIR is a first-level directory or '' for the root. Never mounts.
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fatwrite import Fat32RW, fat_time


def lfn_checksum(short11):
    s = 0
    for b in short11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xff
    return s


class Group:
    """One directory entry: its long-name slots (may be empty) and its 8.3 entry."""

    def __init__(self, lfn, short):
        self.lfn = lfn
        self.short = bytearray(short)

    @property
    def name(self):
        if self.lfn:
            s = b''
            for e in reversed(self.lfn):
                s += e[1:11] + e[14:26] + e[28:32]
            return s.decode('utf-16-le').split('\x00')[0].split('￿')[0]
        stem = bytes(self.short[0:8]).decode('latin-1').rstrip()
        ext = bytes(self.short[8:11]).decode('latin-1').rstrip()
        return stem + ('.' + ext if ext else '')

    @property
    def first(self):
        return struct.unpack_from('<H', self.short, 20)[0] << 16 | struct.unpack_from('<H', self.short, 26)[0]

    @property
    def size(self):
        return struct.unpack_from('<I', self.short, 28)[0]

    def bytes(self):
        return b''.join(self.lfn) + bytes(self.short)


class Editor:
    def __init__(self, img):
        self.fs = Fat32RW(img, write=True)
        self.dirs = {}
        self.zeroed = 0

    def load(self, dirname):
        if dirname in self.dirs:
            return self.dirs[dirname]
        fs = self.fs
        clus = fs.dir_first_cluster(dirname)
        raw = b''.join(fs.rd(fs.coff(c), fs.csz) for c in fs.chain(clus))
        groups, lfn = [], []
        for i in range(0, len(raw), 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xe5:
                lfn = []
                continue
            if e[11] == 0x0f:
                if e[0] & 0x40:
                    lfn = []
                lfn.append(e)
                continue
            if lfn and lfn_checksum(e[0:11]) != lfn[0][13]:
                lfn = []                       # orphaned long name, dropped
            groups.append(Group(lfn, e))
            lfn = []
        self.dirs[dirname] = (clus, groups)
        return self.dirs[dirname]

    def find(self, dirname, name):
        _, groups = self.load(dirname)
        for g in groups:
            if g.name == name or g.name.upper() == name.upper():
                return g
        return None

    def zero_chain(self, chain):
        fs = self.fs
        z = b'\0' * fs.csz
        for c in chain:
            fs.wr(fs.coff(c), z)
            fs.set(c, 0)
        self.zeroed += len(chain)

    def delete(self, dirname, name):
        g = self.find(dirname, name)
        assert g is not None, '%s/%s not found' % (dirname, name)
        assert not g.short[11] & 0x10, '%s/%s is a directory' % (dirname, name)
        self.zero_chain(self.fs.chain(g.first) if g.first else [])
        self.dirs[dirname][1].remove(g)
        return g

    def alloc(self, n):
        fs = self.fs
        run, start = 0, None
        for c in range(2, fs.nclus + 2):
            if fs.get(c) == 0:
                if run == 0:
                    start = c
                run += 1
                if run == n:
                    return list(range(start, start + n))
            else:
                run = 0
        take = [c for c in range(2, fs.nclus + 2) if fs.get(c) == 0][:n]
        assert len(take) == n, 'card full'
        return take

    def short_alias(self, dirname, name):
        _, groups = self.load(dirname)
        have = {bytes(g.short[0:11]) for g in groups}
        stem, _, ext = name.rpartition('.')
        if not stem:
            stem, ext = ext, ''
        base = ''.join(ch for ch in stem.upper() if ch.isalnum())
        ext = ''.join(ch for ch in ext.upper() if ch.isalnum())[:3]
        if len(base) <= 8 and stem.upper() == stem and '.' not in stem and name == name.upper():
            cand = base.ljust(8).encode() + ext.ljust(3).encode()
            if cand not in have:
                return cand, False
        for k in range(1, 100):
            tail = '~%d' % k
            cand = (base[:8 - len(tail)] + tail).ljust(8).encode() + ext.ljust(3).encode()
            if cand not in have:
                return cand, True
        raise RuntimeError('no free alias for ' + name)

    def write(self, dirname, name, data):
        fs = self.fs
        ht, hd = fat_time(time.time())
        g = self.find(dirname, name)
        if g is not None:
            self.zero_chain(fs.chain(g.first) if g.first else [])
        else:
            short, need_lfn = self.short_alias(dirname, name)
            e = bytearray(32)
            e[0:11] = short
            e[11] = 0x20
            e[14:16] = ht
            e[16:18] = hd
            lfn = []
            if need_lfn:
                u = name.encode('utf-16-le') + b'\0\0'
                u += b'\xff' * (-len(u) % 26)
                chunks = [u[i:i + 26] for i in range(0, len(u), 26)]
                ck = lfn_checksum(short)
                for k in range(len(chunks), 0, -1):
                    c = chunks[k - 1]
                    le = bytearray(32)
                    le[0] = k | (0x40 if k == len(chunks) else 0)
                    le[1:11] = c[0:10]
                    le[11] = 0x0f
                    le[13] = ck
                    le[14:26] = c[10:22]
                    le[28:32] = c[22:26]
                    lfn.append(bytes(le))
            g = Group(lfn, e)
            self.dirs[dirname][1].append(g)
        n = -(-len(data) // fs.csz)
        chain = self.alloc(n) if n else []
        for k, c in enumerate(chain):
            fs.set(c, 0x0fffffff if k == n - 1 else chain[k + 1])
            blk = data[k * fs.csz:(k + 1) * fs.csz]
            fs.wr(fs.coff(c), blk + b'\0' * (fs.csz - len(blk)))
        first = chain[0] if chain else 0
        struct.pack_into('<H', g.short, 20, first >> 16)
        struct.pack_into('<H', g.short, 26, first & 0xffff)
        struct.pack_into('<I', g.short, 28, len(data))
        g.short[22:24] = ht
        g.short[24:26] = hd
        g.short[18:20] = hd
        return g

    def flush_dir(self, dirname):
        fs = self.fs
        clus, groups = self.dirs[dirname]
        chain = fs.chain(clus)
        raw = b''.join(g.bytes() for g in groups)
        cap = len(chain) * fs.csz
        assert len(raw) < cap, '%s needs %d bytes, has %d' % (dirname, len(raw), cap)
        raw += b'\0' * (cap - len(raw))
        for k, c in enumerate(chain):
            fs.wr(fs.coff(c), raw[k * fs.csz:(k + 1) * fs.csz])

    def zero_free(self):
        fs = self.fs
        z = b'\0' * fs.csz
        n = 0
        for c in range(2, fs.nclus + 2):
            if fs.get(c) == 0:
                fs.wr(fs.coff(c), z)
                n += 1
        return n

    def close(self):
        fs = self.fs
        for d in self.dirs:
            self.flush_dir(d)
        for i in range(fs.nfat):
            fs.wr(fs.fat0 + i * fs.spf * fs.bps, bytes(fs.fat))
        free = fs.nfree()
        fi = bytearray(fs.rd(fs.base + fs.fsinfo_sec * fs.bps, 512))
        struct.pack_into('<I', fi, 488, free)
        struct.pack_into('<I', fi, 492, 0xffffffff)
        fs.wr(fs.base + fs.fsinfo_sec * fs.bps, bytes(fi))
        fs.f.flush()
        os.fsync(fs.f.fileno())
        fs.f.close()
        return free


def main(argv):
    img = argv[0]
    assert argv[1] == '--write'
    ed = Editor(img)
    zero_free = False
    args = argv[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--zero-free':
            zero_free = True
        elif a == '--compact':
            i += 1
            ed.load(args[i])
        else:
            d, rest = a.split(':', 1)
            if rest.startswith('-'):
                g = ed.delete(d, rest[1:])
                print('deleted %s/%s %d B' % (d, g.name, g.size))
            else:
                n, src = rest.split('=', 1)
                data = open(src, 'rb').read()
                ed.write(d, n, data)
                print('wrote   %s/%s %d B' % (d, n, len(data)))
        i += 1
    if zero_free:
        print('zeroed %d free clusters' % ed.zero_free())
    free = ed.close()
    print('%d clusters zeroed by deletes, %d free after' % (ed.zeroed, free))


if __name__ == '__main__':
    main(sys.argv[1:])
