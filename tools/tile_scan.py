#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Walk a map tile without loading it. `clarion_tile.read_tile` builds the whole container as objects, costing roughly seven times the file size; on the factory tile B48R060R (477 MB) the obvious scan reached 45 GB and swapped the machine. This walks the same structure through mmap, one record at a time, for counting, tallying and searching; use `clarion_tile` to write."""
import mmap, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarion_format

SLOTS = 32
LEVEL_OFF, BBOX_OFF, SECT_OFF, TABLE1_OFF = 0x1ac, 0x2ac, 0x800, 0x1000


def addr_of(v):
    return (v >> 8) * 2048 + (v & 0xff) * 256


ABSENT = (0xffffffff, 0x7fffffff)
EXT_BYTES = 512


def is_extended(ln, x):
    """Whether a slot is extended, from its first pair. An extended slot is EXT_BYTES longer:
    every road pair is followed by two more, which hold the u16 compressed and plain lengths of
    up to four extra road records when the road pair's length has bit 31 set. The first pair
    is flagged, or reads ffffffff 7fffffff when that road record is absent. A directory's kind
    word grows by 0x100 per extended slot. Measured on both formats: every directory length on
    eleven QY7 tiles and three QY8 ones matches its kind word, and every record inflates."""
    return (ln not in ABSENT and ln >> 31) or (ln, x) == (0xffffffff, 0x7fffffff)


class Rec:
    """One record. Holds offsets, not bytes. k is the pair index in the slot; sub is 1 to 4 for
    the extra records of a flagged road pair in an extended slot, else 0."""

    __slots__ = ('tile', 'section', 'level', 'dir_off', 'slot', 'k', 'off', 'n', 'words', 'ext', 'sub')

    def __init__(self, tile, section, level, dir_off, slot, k, off, n, words, ext=False, sub=0):
        self.tile, self.section, self.level = tile, section, level
        self.dir_off, self.slot, self.k = dir_off, slot, k
        self.off, self.n, self.words = off, n, words
        self.ext, self.sub = ext, sub

    @property
    def per_col(self):
        """Pairs per parcel column: section 1 holds a road and a background pair."""
        return (4 if self.section == 1 else 3) if self.ext else (2 if self.section == 1 else 1)

    @property
    def col(self):
        return self.k // self.per_col

    @property
    def is_road_pair(self):
        """In section 1 the road pair comes first in its column and the background pair last."""
        return self.section != 1 or self.k % self.per_col == 0

    @property
    def is_zlib(self):
        return self.n >= 2 and self.tile.fmt.is_zlib(self.tile.m[self.off:self.off + 2])

    def raw(self):
        return bytes(self.tile.m[self.off:self.off + self.n])

    def plain(self):
        """Decompressed body, or the raw bytes if the record is not compressed."""
        return self.tile.fmt.inflate(self.raw())

    def kind(self):
        """The u16 at +4 of the decompressed body: 0x00fa is a road-shape record."""
        p = self.plain()
        return struct.unpack_from('>H', p, 4)[0] if len(p) >= 6 else None

    def __repr__(self):
        return ('Rec(sec=%s lev=%s slot=%d k=%d%s off=%#x n=%d)'
                % (self.section, self.level, self.slot, self.k, '.%d' % self.sub if self.sub else '', self.off, self.n))


class TileScan:
    def __init__(self, path):
        """path, or the tile's bytes when it was read off a card image."""
        if isinstance(path, (bytes, bytearray, memoryview)):
            self.f, self.m = None, path
        else:
            self.f = open(path, 'rb')
            self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        self.fmt = clarion_format.of_header(self.m[:TABLE1_OFF])

    def close(self):
        if self.f:
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

    def slots(self, section=None, level=None):
        """Yield (section, level, dir_off, slot, slot_off, tag, extended, body bytes)."""
        types = {(sid, e[0]): e[1] for sid, lv in self._walk(LEVEL_OFF, 4) for e in lv}
        for sid, lev, doff, nslots in self._dirs(section):
            if level is not None and lev != level:
                continue
            body = clarion_format.slot_body(sid, types[(sid, lev)])   # not the kind word
            p = doff + 12
            for slot in range(nslots):
                tag, ln, x = struct.unpack_from('>III', self.m, p)
                ext = lev in (1, 2) and bool(is_extended(ln, x))   # measured on tile levels only
                b = body + (EXT_BYTES if ext else 0)
                yield sid, lev, doff, slot, p, tag, ext, b
                p += 4 + b

    def records(self, section=None, level=None, even_only=False):
        """Yield Rec for every record, reading only directory bytes as it goes. even_only keeps
        the road pairs of section 1."""
        for sid, lev, doff, slot, p, tag, ext, b in self.slots(section, level):
            if tag == 0xffffffff:
                continue
            q = addr_of(tag)
            per = (4 if sid == 1 else 3) if ext else 1
            k = 0
            while k < b // 8:
                ln, X = struct.unpack_from('>II', self.m, p + 4 + 8 * k)
                lead = ext and k % per == 0
                if ln not in ABSENT:
                    flagged = 0
                    if lev in (1, 2):   # the coarse levels keep the old reading until measured
                        flagged, ln = ln >> 31, ln & 0x7fffffff
                    road = sid != 1 or k % (per if ext else 2) == 0
                    n = 2 * ln if self.fmt.is_zlib(self.m[q:q + 2]) else 2 * (ln + X)
                    if road or not even_only:
                        yield Rec(self, sid, lev, doff, slot, k, q, n, X, ext)
                    q += n
                    if lead and flagged:
                        lens = struct.unpack_from('>4H', self.m, p + 4 + 8 * (k + 1))
                        words = struct.unpack_from('>4H', self.m, p + 4 + 8 * (k + 2))
                        for j in range(4):
                            if lens[j] == 0xffff:
                                continue
                            n = 2 * lens[j] if self.fmt.is_zlib(self.m[q:q + 2]) else 2 * (lens[j] + words[j])
                            yield Rec(self, sid, lev, doff, slot, k, q, n, words[j], ext, j + 1)
                            q += n
                k += 3 if lead else 1


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
