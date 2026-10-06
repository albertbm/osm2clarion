#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Walk a map tile without loading it.

`clarion_tile.read_tile` builds the whole container as objects so it can be edited
and written back. That costs roughly seven times the file size, because every
record keeps its bytes and its words as Python ints. Fine for our Iceland tiles at
a few MB; fatal on a factory tile, where B48R060R is 477 MB and the obvious scan
reached 45 GB and drove the machine into swap.

This reads the same structure through mmap and yields one record at a time. Use it
for counting, tallying and searching. Use `clarion_tile` when you need to write.

    from tile_scan import TileScan
    with TileScan('B48R060R.DAT') as t:
        for r in t.records(section=1, even_only=True):
            if r.is_zlib:
                plain = r.plain()        # decompressed only when asked
"""
import mmap, struct, zlib

SLOTS = 32
LEVEL_OFF, BBOX_OFF, SECT_OFF, TABLE1_OFF = 0x1ac, 0x2ac, 0x800, 0x1000


def addr_of(v):
    return (v >> 8) * 2048 + (v & 0xff) * 256


class Rec:
    """One record. Holds offsets, not bytes."""

    __slots__ = ('tile', 'section', 'level', 'dir_off', 'slot', 'k', 'off', 'n', 'words')

    def __init__(self, tile, section, level, dir_off, slot, k, off, n, words):
        self.tile, self.section, self.level = tile, section, level
        self.dir_off, self.slot, self.k = dir_off, slot, k
        self.off, self.n, self.words = off, n, words

    @property
    def is_zlib(self):
        return self.tile.m[self.off:self.off + 2] == b'\x58\x85'

    def raw(self):
        return bytes(self.tile.m[self.off:self.off + self.n])

    def plain(self):
        """Decompressed body, or the raw bytes if the record is not compressed."""
        return zlib.decompress(self.raw()) if self.is_zlib else self.raw()

    def kind(self):
        """The u16 at +4 of the decompressed body: 0x00fa is a road-shape record."""
        p = self.plain()
        return struct.unpack_from('>H', p, 4)[0] if len(p) >= 6 else None

    def __repr__(self):
        return ('Rec(sec=%s lev=%s slot=%d k=%d off=%#x n=%d)'
                % (self.section, self.level, self.slot, self.k, self.off, self.n))


class TileScan:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)

    def close(self):
        self.m.close()
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _walk(self, off, elen):
        n = struct.unpack_from('>H', self.m, off)[0]
        p = off + 2
        out = []
        for _ in range(n):
            sid, cnt = self.m[p], self.m[p + 1]
            p += 2
            out.append((sid, [bytes(self.m[p + i * elen:p + (i + 1) * elen]) for i in range(cnt)]))
            p += cnt * elen
        return out

    def sections(self):
        """[(section_id, [level numbers])] from the level table."""
        return [(sid, [lv[0] for lv in levels]) for sid, levels in self._walk(LEVEL_OFF, 4)]

    def _dirs(self, section=None):
        """Yield (section_id, level, dir_off, nslots). Mirrors clarion_tile.read_tile."""
        levels = self._walk(LEVEL_OFF, 4)
        entries, p = [], SECT_OFF
        while True:
            a, c = struct.unpack_from('>IH', self.m, p)
            p += 6
            if a == 0xffffffff:
                break
            entries.append((addr_of(a), c))
        for (sid, lv), (taddr, _count) in zip(levels, entries):
            if section is not None and sid != section:
                continue
            nw = struct.unpack_from('>H', self.m, taddr)[0]
            w = struct.unpack_from('>%dH' % nw, self.m, taddr)
            for li, e in enumerate(lv):
                level, cnt = e[0], struct.unpack('>H', e[2:])[0]
                o = w[1 + li]
                if o == 0xffff:
                    continue
                glen, gcnt = w[o], w[o + 1]
                assert glen == 2 + 6 * gcnt and gcnt == cnt, (glen, gcnt, cnt)
                for k in range(gcnt):
                    z, tg = w[o + 6 + 6 * k], w[o + 7 + 6 * k]
                    yield sid, level, addr_of((z << 16) | tg), (16 if level == 3 else SLOTS)

    def records(self, section=None, level=None, even_only=False):
        """Yield Rec for every record, reading only directory bytes as it goes."""
        for sid, lev, doff, nslots in self._dirs(section):
            if level is not None and lev != level:
                continue
            kind = struct.unpack_from('>H', self.m, doff)[0]
            body = 512 if kind == 0x2046 else 256
            p = doff + 12
            for slot in range(nslots):
                tag = struct.unpack_from('>I', self.m, p)[0]
                if tag != 0xffffffff:
                    q = addr_of(tag)
                    for k in range(body // 8):
                        ln, X = struct.unpack_from('>II', self.m, p + 4 + 8 * k)
                        if ln in (0xffffffff, 0x7fffffff):
                            continue
                        n = 2 * ln if self.m[q:q + 2] == b'\x58\x85' else 2 * (ln + X)
                        if not (even_only and k % 2):
                            yield Rec(self, sid, lev, doff, slot, k, q, n, X)
                        q += n
                p += 4 + body


if __name__ == '__main__':
    import sys, collections
    for path in sys.argv[1:]:
        with TileScan(path) as t:
            n = collections.Counter()
            for r in t.records():
                n[(r.section, r.level)] += 1
            print(path.split('/')[-1], dict(t.sections()))
            for (sid, lev), c in sorted(n.items()):
                print('   section %s level %s: %d records' % (sid, lev, c))
