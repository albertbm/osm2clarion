#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write files into a FAT32 partition of a raw card image. Never mounts.

`DIR:NAME=SRC` replaces: the target must already exist as a short-name (8.3) file in the
directory given. The old cluster chain is freed, a new one is allocated contiguously where
the free space allows, the data is written, and the directory entry's size, first cluster
and modification time are updated.

`DIR:+NAME=SRC` creates a file that is not there yet - a card that never carried a
tile for a cell needs it created rather than replaced. A new 8.3
entry is appended after the directory's last live entry, where the factory carries
its own. The slot must already exist in the directory's cluster chain and be zero;
extending a directory is deliberately not implemented, and the call fails rather than
guessing. The `+` is required, so a replace can never silently create.

`DIR:-NAME` deletes: the entry's first byte is set to 0xe5, its cluster chain is freed in
both FAT copies, and nothing else in the directory is touched - the factory's own shape
for a file that was never there is no entry at all, not a zeroed one.

Both FAT copies are rewritten and the FSInfo free count and next-free hint are refreshed.

    fatwrite.py IMAGE --dry-run|--write  DIR:NAME=SRC [DIR:+NAME=SRC ...] [DIR:-NAME ...]

DIR is '' for the root. Nothing else in the filesystem is touched.
"""
import os
import struct
import sys
import time


class Fat32RW:
    def __init__(self, path, part=1, write=False):
        self.f = open(path, 'r+b' if write else 'rb')
        self.write_mode = write
        mbr = self.rd(446 + 16 * (part - 1), 16)
        self.base = struct.unpack_from('<I', mbr, 8)[0] * 512
        b = self.rd(self.base, 512)
        self.bps = struct.unpack_from('<H', b, 11)[0]
        self.spc = b[13]
        self.res = struct.unpack_from('<H', b, 14)[0]
        self.nfat = b[16]
        self.spf = struct.unpack_from('<I', b, 36)[0]
        self.root = struct.unpack_from('<I', b, 44)[0]
        self.fsinfo_sec = struct.unpack_from('<H', b, 48)[0]
        totsec = struct.unpack_from('<I', b, 32)[0]
        self.csz = self.spc * self.bps
        self.fat0 = self.base + self.res * self.bps
        self.data = self.fat0 + self.nfat * self.spf * self.bps
        self.nclus = (totsec - self.res - self.nfat * self.spf) // self.spc
        self.fat = bytearray(self.rd(self.fat0, self.spf * self.bps))
        assert bytes(self.fat) == self.rd(self.fat0 + self.spf * self.bps, self.spf * self.bps), 'the two FAT copies differ'

    def rd(self, off, n):
        self.f.seek(off); d = self.f.read(n)
        assert len(d) == n, 'short read at %#x' % off
        return d

    def wr(self, off, d):
        assert self.write_mode
        self.f.seek(off); self.f.write(d)

    def get(self, c):
        return struct.unpack_from('<I', self.fat, 4 * c)[0] & 0x0fffffff

    def set(self, c, v):
        old = struct.unpack_from('<I', self.fat, 4 * c)[0]
        struct.pack_into('<I', self.fat, 4 * c, (old & 0xf0000000) | (v & 0x0fffffff))

    def chain(self, c):
        out = []
        while 2 <= c < 0x0ffffff8:
            out.append(c); c = self.get(c)
            assert len(out) < 8_000_000, 'FAT chain runaway'
        return out

    def coff(self, c):
        return self.data + (c - 2) * self.csz

    def free_clusters(self):
        return [c for c in range(2, self.nclus + 2) if self.get(c) == 0]

    def nfree(self):
        return sum(1 for c in range(2, self.nclus + 2) if self.get(c) == 0)

    # --- directories -------------------------------------------------------
    def dir_first_cluster(self, name):
        """Cluster of a first-level subdirectory of the root, or the root itself for ''."""
        if not name:
            return self.root
        for off, e in self.entries(self.root):
            if e[11] & 0x10 and self.short(e) == name:
                return struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
        raise KeyError(name)

    def entries(self, cluster):
        """Yield (absolute byte offset, 32-byte entry) for live short-name entries."""
        for c in self.chain(cluster):
            blk = self.rd(self.coff(c), self.csz)
            for i in range(0, self.csz, 32):
                e = blk[i:i + 32]
                if e[0] == 0x00: return
                if e[0] == 0xe5 or e[11] == 0x0f or (e[11] & 0x08 and not e[11] & 0x10): continue
                yield self.coff(c) + i, e

    @staticmethod
    def short(e):
        stem = e[0:8].decode('latin-1').rstrip()
        ext = e[8:11].decode('latin-1').rstrip()
        return stem + ('.' + ext if ext else '')

    def find(self, dirname, fname):
        for off, e in self.entries(self.dir_first_cluster(dirname)):
            if not e[11] & 0x10 and self.short(e) == fname:
                return off, e
        raise KeyError('%s/%s' % (dirname, fname))

    def dir_end(self, dirname):
        """Byte offset of the 0x00 terminator that ends a directory, and the slots left."""
        clus = self.dir_first_cluster(dirname)
        chain = self.chain(clus)
        for n, c in enumerate(chain):
            blk = self.rd(self.coff(c), self.csz)
            for i in range(0, self.csz, 32):
                if blk[i] == 0x00:
                    left = (self.csz - i) // 32 + (len(chain) - n - 1) * (self.csz // 32)
                    return self.coff(c) + i, left
        return None, 0

    def free_slots(self, dirname, names):
        """Entry offsets for new files, appended after the last live entry."""
        off, left = self.dir_end(dirname)
        assert off is not None, '%s has no terminator; directory is full' % dirname
        assert left > len(names), ('%s has %d free entry slots, %d wanted plus a terminator'
                                   % (dirname, left, len(names)))
        have = {self.short(e) for _, e in self.entries(self.dir_first_cluster(dirname))}
        out = {}
        for k, n in enumerate(names):
            assert n not in have, '%s/%s already exists; use a replace, not a create' % (dirname, n)
            stem, _, ext = n.partition('.')
            assert len(stem) <= 8 and len(ext) <= 3 and n == n.upper(), 'not an 8.3 upper-case name: %s' % n
            slot = off + 32 * k
            assert self.rd(slot, 32) == b'\0' * 32, 'entry slot %#x is not zero' % slot
            out[n] = slot
        return out


def plan(fs, jobs):
    """Free the old chains, then allocate for each job. Returns [(dirname, fname, src, off,
    old_chain, new_chain, size)] without writing anything."""
    out = []
    slots = {}
    for dirname, fname, src, create in jobs:
        if create:
            slots.setdefault(dirname, []).append(fname)
    for dirname, names in slots.items():
        slots[dirname] = fs.free_slots(dirname, names)
    for dirname, fname, src, create in jobs:
        if create:
            out.append(dict(dir=dirname, name=fname, src=src, ent=slots[dirname][fname],
                            old=[], oldsize=0, newsize=os.path.getsize(src), create=True))
            continue
        off, e = fs.find(dirname, fname)
        fc = struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
        old = fs.chain(fc)
        size = struct.unpack_from('<I', e, 28)[0]
        out.append(dict(dir=dirname, name=fname, src=src, ent=off, old=old, oldsize=size,
                        newsize=os.path.getsize(src), create=False))
    for j in out:                      # free every old chain first, so the space can be reused
        for c in j['old']: fs.set(c, 0)
    free = fs.free_clusters()
    runs = []                          # contiguous free runs, longest first
    for c in free:
        if runs and c == runs[-1][0] + runs[-1][1]: runs[-1][1] += 1
        else: runs.append([c, 1])
    for j in sorted(out, key=lambda j: -j['newsize']):
        need = -(-j['newsize'] // fs.csz)
        fit = min((r for r in runs if r[1] >= need), key=lambda r: r[1], default=None)
        if fit is None:                # no single run is big enough: take clusters in order
            take = []
            for r in runs:
                while r[1] and len(take) < need:
                    take.append(r[0]); r[0] += 1; r[1] -= 1
                if len(take) == need: break
            assert len(take) == need, 'not enough free space for %s' % j['name']
            j['new'] = take
        else:
            j['new'] = list(range(fit[0], fit[0] + need))
            fit[0] += need; fit[1] -= need
        runs = [r for r in runs if r[1]]
        for k, c in enumerate(j['new']):
            fs.set(c, 0x0fffffff if k == len(j['new']) - 1 else j['new'][k + 1])
    return out


def plan_delete(fs, dirname, fname):
    """Look up the entry and its chain, without writing anything."""
    off, e = fs.find(dirname, fname)
    assert not e[11] & 0x10, '%s/%s is a directory, not a file' % (dirname, fname)
    fc = struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
    size = struct.unpack_from('<I', e, 28)[0]
    return dict(dir=dirname, name=fname, ent=off, old=fs.chain(fc), oldsize=size)


def free_chain(fs, job):
    """In-memory only, so a --dry-run preview sees the space back too."""
    for c in job['old']:
        fs.set(c, 0)


def delete(fs, job):
    """The disk-writing half: mark the entry 0xe5. Call after free_chain."""
    e = bytearray(fs.rd(job['ent'], 32))
    e[0] = 0xe5
    fs.wr(job['ent'], bytes(e))


def fat_time(ts):
    t = time.localtime(ts)
    return (struct.pack('<H', (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)),
            struct.pack('<H', ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday))


def main():
    img = sys.argv[1]
    mode = sys.argv[2]
    assert mode in ('--dry-run', '--write')
    jobs, dels = [], []
    for a in sys.argv[3:]:
        d, n = a.split(':', 1)
        if n.startswith('-'):
            dels.append((d, n[1:]))
            continue
        lhs, src = a.split('=', 1)
        d, n = lhs.split(':', 1)
        create = n.startswith('+')
        jobs.append((d, n.lstrip('+'), src, create))
    fs = Fat32RW(img, write=(mode == '--write'))
    before = fs.nfree()
    print('%s  cluster %d B  %d clusters  %d free (%d B)' % (img, fs.csz, fs.nclus, before, before * fs.csz))
    dp = [plan_delete(fs, d, n) for d, n in dels]
    for j in dp:
        free_chain(fs, j)
        print('  %-9s %-14s %10d -> deleted   %3d clusters freed'
              % (j['dir'] or '/', j['name'], j['oldsize'], len(j['old'])))
    p = plan(fs, jobs)
    after = fs.nfree()
    for j in p:
        print('  %-9s %-14s %10d -> %10d B   %3d -> %3d clusters   %-6s %s' % (
            j['dir'] or '/', j['name'], j['oldsize'], j['newsize'], len(j['old']), len(j['new']),
            'create' if j['create'] else '',
            'contiguous at %d' % j['new'][0] if j['new'] == list(range(j['new'][0], j['new'][0] + len(j['new'])))
            else '%d fragments' % sum(1 for k in range(1, len(j['new'])) if j['new'][k] != j['new'][k-1] + 1)))
    print('  free clusters %d -> %d  (%d B free after)' % (before, after, after * fs.csz))
    if mode == '--dry-run':
        print('  dry run, nothing written'); return
    for j in dp:
        delete(fs, j)
        print('  deleted %s/%s  entry %#x' % (j['dir'], j['name'], j['ent']))
    ht, hd = fat_time(time.time())
    for j in p:
        with open(j['src'], 'rb') as sf:
            for k, c in enumerate(j['new']):
                blk = sf.read(fs.csz)
                if len(blk) < fs.csz: blk += b'\0' * (fs.csz - len(blk))
                fs.wr(fs.coff(c), blk)
        if j['create']:
            stem, _, ext = j['name'].partition('.')
            e = bytearray(32)
            e[0:11] = stem.ljust(8).encode('ascii') + ext.ljust(3).encode('ascii')
            e[11] = 0x20                              # archive, a plain file
            e[14:16] = ht; e[16:18] = hd              # creation time / date
        else:
            e = bytearray(fs.rd(j['ent'], 32))
        struct.pack_into('<I', e, 28, j['newsize'])
        struct.pack_into('<H', e, 26, j['new'][0] & 0xffff)
        struct.pack_into('<H', e, 20, j['new'][0] >> 16)
        e[22:24] = ht; e[24:26] = hd                  # last write time / date
        e[18:20] = hd                                  # last access date
        fs.wr(j['ent'], bytes(e))
        print('  %s %s/%s  %d B at cluster %d, entry %#x'
              % ('created' if j['create'] else 'wrote  ', j['dir'], j['name'],
                 j['newsize'], j['new'][0], j['ent']))
    for i in range(fs.nfat):
        fs.wr(fs.fat0 + i * fs.spf * fs.bps, bytes(fs.fat))
    fi = bytearray(fs.rd(fs.base + fs.fsinfo_sec * fs.bps, 512))
    struct.pack_into('<I', fi, 488, after)
    if p:
        struct.pack_into('<I', fi, 492, max(j['new'][-1] for j in p))
    elif dp:
        struct.pack_into('<I', fi, 492, min(c for j in dp for c in j['old']))
    fs.wr(fs.base + fs.fsinfo_sec * fs.bps, bytes(fi))
    fs.f.flush(); os.fsync(fs.f.fileno()); fs.f.close()
    print('  %d FAT copies and FSInfo updated, fsynced' % fs.nfat)


if __name__ == '__main__':
    main()
