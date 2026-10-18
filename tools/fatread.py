#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only FAT12/16/32 walker over a raw card image. Never mounts, never writes.

`Fat` is the low-level walker: partitions, directories, cluster chains. `Fat32` wraps it
with the whole-tree walk and per-file md5 that `imgcmp.py` wants. Both are read-only;
`fatwrite.py` is the only thing here that writes.
"""
import struct


class Fat:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        mbr = self.rd(0, 512)
        self.parts = []
        for i in range(4):
            e = mbr[446 + 16 * i:446 + 16 * i + 16]
            ptype = e[4]
            lba, cnt = struct.unpack_from('<II', e, 8)
            if ptype and cnt:
                self.parts.append((i, ptype, lba, cnt))

    def rd(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def open_part(self, lba):
        self.base = lba * 512
        b = self.rd(self.base, 512)
        self.bps = struct.unpack_from('<H', b, 11)[0]
        self.spc = b[13]
        self.resv = struct.unpack_from('<H', b, 14)[0]
        self.nfat = b[16]
        self.rootents = struct.unpack_from('<H', b, 17)[0]
        self.fatsz16 = struct.unpack_from('<H', b, 22)[0]
        self.fatsz32 = struct.unpack_from('<I', b, 36)[0]
        self.fatsz = self.fatsz16 or self.fatsz32
        self.fat32 = self.fatsz16 == 0
        self.fatstart = lba + self.resv
        self.rootdirsec = self.fatstart + self.nfat * self.fatsz
        rootsec = (self.rootents * 32 + self.bps - 1) // self.bps
        self.datastart = self.rootdirsec + rootsec
        if self.fat32:
            self.rootclus = struct.unpack_from('<I', b, 44)[0]
            self.label = b[71:82].decode('latin1')
            self.fstype = b[82:90].decode('latin1')
        else:
            self.rootclus = 0
            self.label = b[43:54].decode('latin1')
            self.fstype = b[54:62].decode('latin1')

    def csec(self, c):
        return self.datastart + (c - 2) * self.spc

    def fat_entry(self, c):
        if self.fat32:
            off = self.fatstart * self.bps + c * 4
            return struct.unpack('<I', self.rd(off, 4))[0] & 0x0fffffff
        off = self.fatstart * self.bps + c * 2
        return struct.unpack('<H', self.rd(off, 2))[0]

    def chain(self, c):
        out = []
        end = 0x0ffffff8 if self.fat32 else 0xfff8
        while c and c < end and len(out) < 1 << 22:
            out.append(c)
            c = self.fat_entry(c)
        return out

    def read_clusters(self, clus, size=None):
        parts = []
        n = 0
        for c in self.chain(clus):
            d = self.rd(self.csec(c) * self.bps, self.spc * self.bps)
            parts.append(d)
            n += len(d)
            if size is not None and n >= size:
                break
        d = b''.join(parts)
        return d[:size] if size is not None else d

    def readdir(self, clus=None):
        """[(name, isdir, cluster, size)] with long names assembled."""
        if clus is None:
            clus = self.rootclus if self.fat32 else 0
        data = self.read_clusters(clus) if clus else self.rd(self.rootdirsec * self.bps,
                                                            self.rootents * 32)
        ents, lfn = [], {}
        for i in range(0, len(data), 32):
            e = data[i:i + 32]
            if len(e) < 32 or e[0] == 0:
                break
            if e[0] == 0xe5:
                lfn = {}
                continue
            if e[11] == 0x0f:
                seq = e[0] & 0x3f
                s = (e[1:11] + e[14:26] + e[28:32]).decode('utf-16-le', 'replace')
                lfn[seq] = s.split('￿')[0].split('\x00')[0]
                continue
            if e[11] & 0x08 and not e[11] & 0x10:
                lfn = {}
                continue
            if lfn:
                full = ''.join(lfn[k] for k in sorted(lfn))
            else:
                nm = e[0:8].decode('latin1').rstrip()
                ext = e[8:11].decode('latin1').rstrip()
                full = nm + ('.' + ext if ext else '')
            lfn = {}
            if full in ('.', '..'):
                continue
            c = struct.unpack('<H', e[20:22])[0] << 16 | struct.unpack('<H', e[26:28])[0]
            sz = struct.unpack('<I', e[28:32])[0]
            ents.append((full, bool(e[11] & 0x10), c, sz))
        return ents


class Fat32:
    """Whole-image view: walk every file in the first partition, md5 on demand."""

    def __init__(self, path):
        self.f = Fat(path)
        self.f.open_part(self.f.parts[0][2])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        self.f.f.close()

    @staticmethod
    def _mtime(e):
        t, d = struct.unpack_from('<HH', e, 22)
        return '%04d-%02d-%02d %02d:%02d:%02d' % (1980 + (d >> 9), (d >> 5) & 15, d & 31,
                                                  t >> 11, (t >> 5) & 63, (t & 31) * 2)

    def _readdir(self, clus):
        """Like Fat.readdir but keeping the raw entry, so mtime survives."""
        f = self.f
        data = f.read_clusters(clus) if clus else f.rd(f.rootdirsec * f.bps, f.rootents * 32)
        out, lfn = [], {}
        for i in range(0, len(data), 32):
            e = data[i:i + 32]
            if len(e) < 32 or e[0] == 0:
                break
            if e[0] == 0xe5:
                lfn = {}
                continue
            if e[11] == 0x0f:
                s = (e[1:11] + e[14:26] + e[28:32]).decode('utf-16-le', 'replace')
                lfn[e[0] & 0x3f] = s.split('\uffff')[0].split('\x00')[0]
                continue
            if e[11] & 0x08 and not e[11] & 0x10:
                lfn = {}
                continue
            if lfn:
                full = ''.join(lfn[k] for k in sorted(lfn))
            else:
                nm = e[0:8].decode('latin1').rstrip()
                ext = e[8:11].decode('latin1').rstrip()
                full = nm + ('.' + ext if ext else '')
            lfn = {}
            if full in ('.', '..'):
                continue
            c = struct.unpack('<H', e[20:22])[0] << 16 | struct.unpack('<H', e[26:28])[0]
            out.append((full, bool(e[11] & 0x10), c,
                        struct.unpack('<I', e[28:32])[0], self._mtime(e)))
        return out

    def walk(self, skip=('System Volume Information', '.fseventsd', '.Spotlight-V100')):
        """(path, size, first cluster, mtime) for every entry; size is None for a directory."""
        queue = [('', None)]
        while queue:
            prefix, clus = queue.pop(0)
            for name, isdir, c, sz, mt in self._readdir(clus if clus else self.f.rootclus):
                path = prefix + '/' + name
                if isdir:
                    yield path, None, c, mt
                    if name not in skip:
                        queue.append((path, c))
                else:
                    yield path, sz, c, mt

    def md5(self, clus, size):
        import hashlib
        h, n, f = hashlib.md5(), 0, self.f
        for c in f.chain(clus):
            d = f.rd(f.csec(c) * f.bps, f.spc * f.bps)
            if n + len(d) > size:
                d = d[:size - n]
            h.update(d)
            n += len(d)
            if n >= size:
                break
        return h.hexdigest()
