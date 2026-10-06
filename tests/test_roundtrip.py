#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Roundtrip checks for the readers and encoders in tools/.

Every input here is built in code, so this needs no card, no OSM extract and nothing
proprietary. It checks two things: that decode(encode(x)) gives x back, and that the
bytes in between match the layout docs/ records.

    python3 -m unittest discover -s tests -v

Only the standard library is imported at module level. The cases that need shapely are
skipped without it, since shapely is needed to build geometry but not to encode a record.
"""
import math
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import bg_decode
import clarion_bg
import clarion_profile
import clarion_rd
import clarion_tile
import fa_encode
import make_drstr
import rd_encode
import tile_grid
import tile_retarget

try:
    import bg_encode
except ImportError:                      # shapely absent
    bg_encode = None

UNITS = clarion_bg.UNITS                 # 2048 parcel units per side


# ---- building inputs ----------------------------------------------------------------
# A shape's vertices are int8 deltas, so a long edge has to be walked in steps of at most
# 127 units. bg_encode.densify does this for real geometry; these two do it for the
# handful of shapes built here, without pulling shapely in.

def steps_to(start, end):
    """Points from start to end in steps of at most 127 units, end included."""
    (x0, y0), (x1, y1) = start, end
    n = max(1, -(-max(abs(x1 - x0), abs(y1 - y0)) // 127))
    return [(x0 + round((x1 - x0) * i / n), y0 + round((y1 - y0) * i / n)) for i in range(1, n + 1)]


def open_line(corners):
    pts = [corners[0]]
    for a, b in zip(corners, corners[1:]):
        pts += steps_to(a, b)
    return pts


def closed_ring(corners):
    """A polygon ring, first vertex repeated last, as the shipped records have it."""
    pts = [corners[0]]
    for a, b in zip(corners, list(corners[1:]) + [corners[0]]):
        pts += steps_to(a, b)
    return pts


FULL_PARCEL_SEA = closed_ring([(0, 0), (0, UNITS - 1), (UNITS - 1, UNITS - 1), (UNITS - 1, 0)])


def quant(v, v0, span):
    """Degrees to parcel units, as bg_encode.quant does it."""
    return max(0, min(UNITS - 1, int(round((v - v0) / span * UNITS))))


def background_record(level, y0, x0, row, col, sea=(), coast=(), sections=None):
    """An inflated background record for one parcel, plus its zlib stream."""
    L = clarion_bg.LEVELS[level]
    per_row, per_col = L['rows'] // L['dir_meshes'], L['cols'] // L['dir_meshes']
    w = [0] * 22
    w[0], w[1] = clarion_bg.parcel_code(level, y0 * per_row + row, x0 * per_col + col)
    w[2] = 0x19
    w[3], w[4] = L['w34']
    w[7], w[8] = 0xffff, 0
    for i in (13, 15, 17):
        w[i], w[i + 1] = 0xffff, 0
    w[19], w[20] = 0, 0xffff
    layers = {}
    if sea:
        layers[1] = dict(kind=0x80, shapes=[dict(type=L['seatype'], ref=0xffff, pts=p) for p in sea])
    if coast:
        layers[11] = dict(kind=0x40, shapes=[dict(type=L['coasttype'], ref=0xffff, pts=p) for p in coast])
    rec = dict(hdr=w, nlayers=26 if layers else 0, layers=layers, sections=sections or {})
    plain = clarion_bg.build_record(rec)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(plain) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    return plain, comp


def background_dir(level, y0, x0, recs):
    """A section-1 directory for clarion_tile.write_tile. Mirrors bg_encode.make_dir,
    which cannot be imported without shapely; TestAgainstBgEncode checks they agree."""
    L = clarion_bg.LEVELS[level]
    body, nslots = L['body'], L['slots']
    npairs = body // 8

    def empty_pair(k):
        return b'\x7f\xff\xff\xff\x7f\xff\xff\xff' if (level == 1 and k % 2 == 0) else b'\xff' * 8

    slots = {}
    for row, cols in sorted(recs.items()):
        pairs = [empty_pair(k) for k in range(npairs)]
        rlist = []
        for col, (plain, comp) in sorted(cols.items()):
            pairs[2 * col + 1] = struct.pack('>II', len(comp) // 2, len(plain) // 2)
            rlist.append(dict(k=2 * col + 1, raw=comp, words=len(plain) // 2))
        slots[row] = dict(body=b''.join(pairs), recs=rlist)
    empty = b''.join(empty_pair(k) for k in range(npairs))
    n = L['dir_meshes']
    return dict(kind=L['kind'], ab=L['ab'], y=y0, x=x0, typ=L['typ'], body=body, nslots=nslots,
                slots=slots, empty_bodies={i: empty for i in range(nslots) if i not in slots},
                box=(y0 + n, y0, x0, x0 + n))


def data_box(level, dirs):
    """The arc-minute box of the parcels a level's directories hold."""
    lo = hi = None
    for D in dirs:
        for row, cols in D['_recs'].items():
            for col in cols:
                a, b, w, h = clarion_bg.parcel_geo(level, D['y'], D['x'], row, col)
                lo = (b, a) if lo is None else (min(lo[0], b), min(lo[1], a))
                hi = (b + h, a + w) if hi is None else (max(hi[0], b + h), max(hi[1], a + w))
    return (math.ceil(hi[0] * 60), math.floor(lo[0] * 60), math.floor(lo[1] * 60), math.ceil(hi[1] * 60))


SEC1_LEVELS = [(6, 0x80), (5, 0x90), (4, 0x91), (3, 0x91), (2, 0x91), (1, 0x92)]
SEC2_LEVELS = [(5, 0x81), (4, 0x81), (2, 0x81), (1, 0x90)]


def tile_header(dirs):
    """A 0x1000 header: the strings, the level table at 0x1ac, the box table at 0x2ac and
    a section table at 0x800. write_tile fills the two section addresses and relies on the
    terminator after them already being there."""
    h = bytearray(b'\xff' * clarion_tile.TABLE1_OFF)
    h[0:14] = 'CLARION'.encode('utf-16-be')
    lv = struct.pack('>H', 2) + bytes([1, len(SEC1_LEVELS)])
    lv += b''.join(struct.pack('>BBH', l, t, len(dirs.get(l, []))) for l, t in SEC1_LEVELS)
    lv += bytes([2, len(SEC2_LEVELS)]) + b''.join(struct.pack('>BBH', l, t, 0) for l, t in SEC2_LEVELS)
    h[0x1ac:0x1ac + len(lv)] = lv
    levels = sorted(dirs, reverse=True)
    bb = struct.pack('>H', 2) + bytes([1, len(levels)])
    bb += b''.join(struct.pack('>BB4h', l, 0, *data_box(l, dirs[l])) for l in levels)
    bb += bytes([2, 0])
    h[0x2ac:0x2ac + len(bb)] = bb
    struct.pack_into('>IHIHI', h, clarion_tile.SECT_OFF, 0, 0, 0, 0, 0xffffffff)
    return h


def build_tile(dirs):
    """Tile bytes from {level: [directory, ...]} for section 1."""
    sec1 = dict(id=1, count=0, levels=[dict(level=l, typ=t, dirs=dirs.get(l, [])) for l, t in SEC1_LEVELS])
    sec2 = dict(id=2, count=0, levels=[dict(level=l, typ=t, dirs=[]) for l, t in SEC2_LEVELS])
    t = dict(header=tile_header(dirs), sections=[sec1, sec2])
    for sec in t['sections']:
        nw, _, _ = clarion_tile.table_words(sec)
        sec['count'] = -(-2 * nw // 256)
    return clarion_tile.write_tile(t)


def iceland_tile():
    """One tile over Iceland's north-west cell, with a level-2 and a level-1 directory."""
    name = 'B60R080R'
    yb, xb = clarion_bg.tile_cell(name)
    coast = [open_line([(100, 100), (900, 700), (1400, 1900)])]
    dirs = {}
    recs2 = {3: {5: background_record(2, yb, xb, 3, 5, [FULL_PARCEL_SEA], coast)}}
    d2 = background_dir(2, yb, xb, recs2)
    d2['_recs'] = recs2
    dirs[2] = [d2]
    recs1 = {7: {9: background_record(1, yb + 1, xb + 2, 7, 9, [FULL_PARCEL_SEA], coast)},
             8: {9: background_record(1, yb + 1, xb + 2, 8, 9, [FULL_PARCEL_SEA])}}
    d1 = background_dir(1, yb + 1, xb + 2, recs1)
    d1['_recs'] = recs1
    dirs[1] = [d1]
    return name, build_tile(dirs)


# ---- the checks ---------------------------------------------------------------------

class TestAddresses(unittest.TestCase):
    """The u32 address form of GEOMETRY.md: (off // 2048) << 8 | (off % 2048) // 256."""

    def test_offset_roundtrip(self):
        for off in range(0, 1 << 22, 256):
            self.assertEqual(clarion_tile.addr_of(clarion_tile.enc_addr(off)), off)

    def test_documented_form(self):
        for off in (0, 0x100, 0x800, 0x1000, 0x2b900):
            self.assertEqual(clarion_tile.enc_addr(off), (off // 2048) << 8 | (off % 2048) // 256)

    def test_unaligned_offset_refused(self):
        with self.assertRaises(AssertionError):
            clarion_tile.enc_addr(0x101)

    def test_pad256(self):
        self.assertEqual(clarion_tile.pad256(1), 256)
        self.assertEqual(clarion_tile.pad256(256), 256)
        self.assertEqual(clarion_tile.pad256(257), 512)


class TestGrid(unittest.TestCase):
    """Tile names are hexadecimal: lat_min = B * 2/3, lon_max = 108 - R (FORMAT.md)."""

    # south-west corner per FORMAT.md; a cell is 8 degrees by 320 arcminutes
    CELLS = {'B58R078R': (58.67, -20.0), 'B58R080R': (58.67, -28.0),
             'B60R078R': (64.0, -20.0), 'B60R080R': (64.0, -28.0),
             'B48R060R': (48.0, 4.0), 'B30R060R': (32.0, 4.0)}

    def test_name_to_cell(self):
        for name, (lat_min, lon_min) in self.CELLS.items():
            y0, x0 = clarion_bg.tile_cell(name)
            self.assertAlmostEqual(clarion_bg.mesh_to_lat(y0), lat_min, places=2)
            self.assertEqual(clarion_bg.mesh_to_lon(x0), lon_min)
            self.assertEqual(lon_min + 8, 108 - int(name[4:7], 16), 'lon_max = 108 - R')
            self.assertAlmostEqual(clarion_bg.mesh_to_lat(y0), int(name[1:3], 16) * 2 / 3)

    def test_mesh_formula(self):
        # y = lat * 1.5 and x = lon + 924, so a mesh is 1 deg by 40 arcmin
        for lon in range(-28, 69):
            self.assertEqual(clarion_bg.mesh_to_lon(lon + 924), lon)
        self.assertAlmostEqual(clarion_bg.mesh_to_lat(96), 64.0)

    def test_declared_box_inside_predicted_cell(self):
        """tile_grid reads a tile's own box out of its header and checks the formula."""
        name, blob = iceland_tile()
        with tempfile.TemporaryDirectory() as d:
            Path(d, name + '.DAT').write_bytes(blob)
            rows = tile_grid.boxes(d)
        self.assertEqual(len(rows), 1)
        f, B, R, latlo, lathi, lonlo, lonhi, size = rows[0]
        self.assertEqual((B, R), (0x60, 0x080))
        self.assertLess(abs(latlo - B * 2 / 3), 5.4)
        self.assertLess(abs(lonhi - (108 - R)), 8.1)
        self.assertEqual(size, len(blob))


class TestParcelCodes(unittest.TestCase):
    """clarion_bg.parcel_code against the formulas written out in the docs. The docs give
    them over the European box, where column >> 8 is 1 and supplies w0's top bit."""

    @staticmethod
    def doc_level2(row, col):
        return (0x8000 | (row >> 4) << 8 | (row & 15) << 4 | 0xf,
                ((col >> 4) & 0xff) << 8 | (col & 15) << 4 | 2)

    @staticmethod
    def doc_level1(row, col):
        return (0x8000 | (row >> 6) << 8 | ((row & 63) >> 2) << 4 | 0xc | (row & 3),
                ((col >> 6) & 0xff) << 8 | ((col & 63) >> 2) << 4 | 0xc | (col & 3))

    @staticmethod
    def doc_level3(y_mesh, x_mesh, row, col):
        row3, col3 = 2 * y_mesh + row, 2 * x_mesh + col
        return (0x8000 | (row3 >> 2) << 8 | (row3 & 2) << 6 | (row3 & 1) << 4 | 0xf,
                ((col3 >> 2) & 0xff) << 8 | (col3 & 2) << 6 | (col3 & 1) << 4 | 0x3)

    def test_levels_1_2_3(self):
        checked = 0
        for ym in range(32, 121, 4):          # the database runs y 32 to 120
            for xm in range(896, 993, 3):     # x 896 (-28 deg) to 992 (68 deg)
                for slot in (0, 5, 31):
                    row, col = ym * 8 + slot % 8, xm * 8 + slot % 8
                    self.assertEqual(clarion_bg.parcel_code(2, row, col), self.doc_level2(row, col))
                    row, col = ym * 32 + slot, xm * 32 + slot
                    self.assertEqual(clarion_bg.parcel_code(1, row, col), self.doc_level1(row, col))
                    for row in range(2):
                        for col in range(2):
                            self.assertEqual(clarion_bg.parcel_code(3, 2 * ym + row, 2 * xm + col),
                                             self.doc_level3(ym, xm, row, col))
                    checked += 6
        self.assertGreater(checked, 5000)

    def test_measured_code(self):
        """BUILD-LOG.md records ac0c cb6c for a shipped open-sea level-1 parcel: mesh
        (88, 918), parcel row 0 column 24."""
        self.assertEqual(clarion_bg.parcel_code(1, 88 * 32, 918 * 32 + 24), (0xac0c, 0xcb6c))

    def test_absolute_not_directory_local(self):
        """The black-map bug: a code built from the directory-local row and column puts the
        parcel near the equator. The absolute code must differ from the local one."""
        y0, x0 = 88, 918
        for row in range(2):
            for col in range(2):
                absolute = clarion_bg.parcel_code(1, y0 * 32 + row, x0 * 32 + col)
                local = clarion_bg.parcel_code(1, row, col)
                self.assertNotEqual(absolute, local)
        self.assertEqual(clarion_bg.parcel_code(1, 0, 0), (0x000c, 0x000c),
                         'the code the first build wrote, which claims a parcel near the equator')


class TestShapeGroups(unittest.TestCase):
    """build_group / parse_group, the layer bodies of GEOMETRY.md."""

    def roundtrip(self, group):
        raw = clarion_bg.build_group(group)
        self.assertEqual(len(raw) % 4, 0, 'a group is padded to a unit boundary')
        back = clarion_bg.parse_group(raw, 0, len(raw))
        self.assertEqual(back, group)
        self.assertEqual(clarion_bg.build_group(back), raw)
        return raw

    def test_polygon(self):
        g = dict(kind=0x80, shapes=[dict(type=0x5012, ref=0xffff, pts=FULL_PARCEL_SEA)])
        raw = self.roundtrip(g)
        self.assertEqual(raw[:4], clarion_bg.MAGIC, 'one sub-group gives the 0001 0003 header')
        self.assertEqual(raw[4], 0x80)
        self.assertEqual(raw[5], 1)

    def test_polyline(self):
        g = dict(kind=0x40, shapes=[dict(type=0x5007, ref=0xffff, pts=open_line([(5, 5), (600, 900)])),
                                    dict(type=0x500a, ref=0xffff, pts=[(1, 2), (3, 4)])])
        raw = self.roundtrip(g)
        self.assertEqual(raw[5], 2)

    def test_points(self):
        g = dict(kind=0x00, shapes=[dict(type=0x100c, ref=0xffff, pts=[(700, 800)])])
        self.roundtrip(g)

    def test_several_sub_groups(self):
        """The widened coarse file forced parse_group to handle layers with sub-groups."""
        g = dict(kind=0x80, shapes=[dict(type=0x8012, ref=0xffff, pts=closed_ring([(0, 0), (0, 300), (300, 300)]))],
                 more=[dict(kind=0x40, shapes=[dict(type=0x8007, ref=0xffff, pts=[(10, 10), (20, 20)])])])
        raw = self.roundtrip(g)
        self.assertEqual(struct.unpack_from('>HH', raw, 0), (2, 2 + 2 * 2 - 1))

    def test_vertex_delta_limit(self):
        """A step wider than an int8 is refused rather than silently truncated."""
        g = dict(kind=0x40, shapes=[dict(type=0x5007, ref=0xffff, pts=[(0, 0), (0, 200)])])
        with self.assertRaises(AssertionError):
            clarion_bg.build_group(g)

    def test_polygon_repeats_first_vertex(self):
        self.assertEqual(FULL_PARCEL_SEA[0], FULL_PARCEL_SEA[-1])


class TestBackgroundRecords(unittest.TestCase):
    """parse_record / build_record on the inflated background record."""

    def test_rebuild_is_byte_exact(self):
        plain, comp = background_record(2, 96, 896, 3, 5, [FULL_PARCEL_SEA],
                                        [open_line([(10, 10), (400, 600)])])
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(clarion_bg.build_record(rec), plain)

    def test_layers_survive(self):
        coast = [open_line([(10, 10), (400, 600)])]
        plain, comp = background_record(1, 96, 896, 3, 5, [FULL_PARCEL_SEA], coast)
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(sorted(rec['layers']), [1, 11], 'sea in layer 1, coastline in layer 11')
        self.assertEqual(rec['layers'][1]['kind'], 0x80)
        self.assertEqual(rec['layers'][11]['kind'], 0x40)
        self.assertEqual(rec['layers'][1]['shapes'][0]['pts'], FULL_PARCEL_SEA)
        self.assertEqual(rec['layers'][11]['shapes'][0]['pts'], coast[0])
        self.assertEqual(rec['layers'][1]['shapes'][0]['type'], 0xc012, 'level 1 uses c0xx types')

    def test_documented_header(self):
        plain, comp = background_record(2, 96, 896, 3, 5, [FULL_PARCEL_SEA])
        w = struct.unpack_from('>22H', plain, 0)
        self.assertEqual(w[2], 0x0019, 'w2 = 0x0019 marks a background record')
        self.assertEqual((w[3], w[4]), (0xffff, 0xffff), 'level 2 w3/w4')
        self.assertEqual((w[5], w[6]), (0, 0))
        self.assertEqual(w[9], 11, 'geometry at unit 11, right after the 44-byte header')
        self.assertEqual(w[11], 0xffff, 'no names section when there are no labels')
        self.assertEqual((w[19], w[20]), (0, 0xffff))
        self.assertEqual(w[21], 0)
        plain1, _ = background_record(1, 96, 896, 3, 5, [FULL_PARCEL_SEA])
        w1 = struct.unpack_from('>22H', plain1, 0)
        self.assertEqual((w1[3], w1[4]), (0x347a, 0x2aef), 'level 1 w3/w4')

    def test_layer_table_length_equals_first_offset(self):
        plain, _ = background_record(2, 96, 896, 3, 5, [FULL_PARCEL_SEA])
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(rec['nlayers'], 26)
        base = 4 * rec['hdr'][9]
        first = min(off for off, ln in (struct.unpack_from('>HH', plain, base + 4 * i)
                                        for i in range(rec['nlayers'])) if off != 0xffff)
        self.assertEqual(first, rec['nlayers'])

    def test_names_section_roundtrip(self):
        names = struct.pack('>HH', 6, 1) + b'\xff\xff\x00\x00' * 5 \
            + struct.pack('>HHHHH', 0x4089, 0x2800, 1000, 1000, 10) + b'ATLANTSHAF'
        plain, _ = background_record(2, 96, 896, 3, 5, [FULL_PARCEL_SEA], sections={'names': names})
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(rec['sections']['names'], names)
        self.assertNotEqual(rec['hdr'][11], 0xffff)
        self.assertEqual(clarion_bg.build_record(rec), plain)

    def test_empty_record_has_no_geometry(self):
        """A land-only parcel ships a header-only record."""
        plain, _ = background_record(2, 96, 896, 3, 5)
        self.assertEqual(len(plain), 44)
        w = struct.unpack_from('>22H', plain, 0)
        self.assertEqual((w[9], w[10]), (0xffff, 0))
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(rec['nlayers'], 0)
        self.assertEqual(clarion_bg.build_record(rec), plain)

    def test_zlib_stream_shape(self):
        """The shipped records are level 6 with an 8 KiB window, odd lengths zero padded."""
        plain, comp = background_record(1, 96, 896, 3, 5, [FULL_PARCEL_SEA])
        self.assertEqual(comp[:2], b'\x58\x85')
        self.assertEqual(len(comp) % 2, 0)
        self.assertEqual(zlib.decompress(comp), plain)


class TestTileContainer(unittest.TestCase):
    """read_tile / write_tile: the whole container."""

    def setUp(self):
        self.name, self.blob = iceland_tile()

    def test_rebuild_is_byte_identical(self):
        t = clarion_tile.read_tile(self.blob)
        self.assertEqual(clarion_tile.write_tile(t), self.blob)

    def test_rebuild_is_stable_over_two_passes(self):
        once = clarion_tile.write_tile(clarion_tile.read_tile(self.blob))
        twice = clarion_tile.write_tile(clarion_tile.read_tile(once))
        self.assertEqual(once, twice)

    def test_structure_survives(self):
        t = clarion_tile.read_tile(self.blob)
        self.assertEqual([s['id'] for s in t['sections']], [1, 2])
        sec1 = t['sections'][0]
        self.assertEqual([L['level'] for L in sec1['levels']], [6, 5, 4, 3, 2, 1])
        by_level = {L['level']: L['dirs'] for L in sec1['levels']}
        self.assertEqual(len(by_level[2]), 1)
        self.assertEqual(len(by_level[1]), 1)
        self.assertEqual(sorted(by_level[1][0]['slots']), [7, 8])
        d2 = by_level[2][0]
        self.assertEqual(d2['kind'], 0x2046)
        self.assertEqual(d2['body'], 512)
        self.assertEqual(d2['nslots'], 32)
        self.assertEqual([r['k'] for r in d2['slots'][3]['recs']], [11], 'pair index is 2*col + 1')

    def test_everything_past_the_header_is_aligned(self):
        t = clarion_tile.read_tile(self.blob)
        for sec in t['sections']:
            self.assertEqual(sec['taddr'] % 256, 0)
            for L in sec['levels']:
                for D in L['dirs']:
                    self.assertEqual(D['off'] % 256, 0)
                    for s in D['slots'].values():
                        self.assertEqual(s['block_off'] % 256, 0)

    def test_section_table(self):
        p = clarion_tile.SECT_OFF
        addrs = []
        while True:
            a, c = struct.unpack_from('>IH', self.blob, p)
            p += 6
            if a == 0xffffffff:
                break
            addrs.append(clarion_tile.addr_of(a))
        self.assertEqual(len(addrs), 2)
        self.assertEqual(addrs[0], clarion_tile.TABLE1_OFF, 'section 1 table opens the body')
        self.assertEqual(sorted(addrs), addrs)

    def test_records_inflate(self):
        t = clarion_tile.read_tile(self.blob)
        n = 0
        for sec in t['sections']:
            for L in sec['levels']:
                for D in L['dirs']:
                    for s in D['slots'].values():
                        for r in s['recs']:
                            plain = zlib.decompress(r['raw'])
                            self.assertEqual(len(plain) // 2, r['words'],
                                             'the slot pair records the inflated word count')
                            n += 1
        self.assertEqual(n, 3)


class TestGeometryRoundtrip(unittest.TestCase):
    """Longitude and latitude in, through a tile, and back out again."""

    def metres(self, lon0, lat0, lon1, lat1):
        return (abs(lon1 - lon0) * 111320 * math.cos(math.radians(lat0)),
                abs(lat1 - lat0) * 111320)

    def test_coast_comes_back_within_a_parcel_unit(self):
        level, y0, x0, row, col = 1, 96, 898, 7, 9
        lon0, lat0, w, h = clarion_bg.parcel_geo(level, y0, x0, row, col)
        # fractions chosen so every step fits an int8 delta and nothing is densified
        want = [(lon0 + w * 0.20, lat0 + h * 0.30), (lon0 + w * 0.25, lat0 + h * 0.35),
                (lon0 + w * 0.30, lat0 + h * 0.40)]
        pts = [(quant(lon, lon0, w), quant(lat, lat0, h)) for lon, lat in want]
        recs = {row: {col: background_record(level, y0, x0, row, col, coast=[pts])}}
        D = background_dir(level, y0, x0, recs)
        D['_recs'] = recs
        blob = build_tile({level: [D]})
        feats = [f for f in bg_decode.decode_tile(blob) if f['layer'] == 11]
        self.assertEqual(len(feats), 1)
        got = feats[0]['coords']
        self.assertEqual(len(got), len(want))
        for (lon_a, lat_a), (lon_b, lat_b) in zip(want, got):
            dx, dy = self.metres(lon_a, lat_a, lon_b, lat_b)
            # half a parcel unit at level 1 is well under a metre; GEOMETRY.md
            # records 1.3 m max over real coastline, which also carries simplify error
            self.assertLess(dx, 2.0)
            self.assertLess(dy, 2.0)

    def test_sea_polygon_fills_the_parcel(self):
        name, blob = iceland_tile()
        sea = [f for f in bg_decode.decode_tile(blob) if f['layer'] == 1]
        self.assertEqual(len(sea), 3)
        for f in sea:
            lon0, lat0, w, h = clarion_bg.parcel_geo(f['level'], f['y0'], f['x0'], f['row'], f['col'])
            lons = [c[0] for c in f['coords']]
            lats = [c[1] for c in f['coords']]
            self.assertAlmostEqual(min(lons), lon0, places=6)
            self.assertAlmostEqual(min(lats), lat0, places=6)
            self.assertLess(max(lons) - (lon0 + w), 1e-4)
            self.assertLess(max(lats) - (lat0 + h), 1e-4)
            self.assertEqual(f['coords'][0], f['coords'][-1])

    def test_decoded_parcel_is_where_the_name_says(self):
        name, blob = iceland_tile()
        y0, x0 = clarion_bg.tile_cell(name)
        west, south = clarion_bg.mesh_to_lon(x0), clarion_bg.mesh_to_lat(y0)
        for f in bg_decode.decode_tile(blob):
            for lon, lat in f['coords']:
                self.assertTrue(west <= lon <= west + 8, (name, lon))
                self.assertTrue(south <= lat <= south + 8 * clarion_bg.MESH_H, (name, lat))

    def test_geojson(self):
        name, blob = iceland_tile()
        gj = bg_decode.to_geojson(bg_decode.decode_tile(blob))
        kinds = sorted({f['geometry']['type'] for f in gj['features']})
        self.assertEqual(kinds, ['LineString', 'Polygon'])


class TestRoadGraph(unittest.TestCase):
    """rd_encode writes a section-2 record, clarion_rd reads it back."""

    NODES = [(100, 200), (900, 240), (1500, 1800), (300, 1000)]
    LINKS = [(0, 1, 850), (1, 2, 1600), (0, 3, 700)]

    def test_nodes_and_links_roundtrip(self):
        plain, comp = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS)
        rec = clarion_rd.parse_record(plain)
        self.assertIsNone(rec.get('error'))
        self.assertEqual(rec['consumed'], rec['unit_len'], 'the node list fills the unit exactly')
        self.assertEqual(rec['nnodes'], len(self.NODES))
        self.assertEqual(rec['nlinks'], 2 * len(self.LINKS), 'links are counted once per end')
        self.assertEqual(sorted((n['x'], n['y']) for n in rec['nodes']), sorted(self.NODES))
        seen, _ = clarion_rd.links(rec)
        self.assertEqual(len(seen), len(self.LINKS))
        got = sorted(tuple(sorted(((rec['nodes'][a]['x'], rec['nodes'][a]['y']),
                                   (rec['nodes'][b]['x'], rec['nodes'][b]['y'])))) + (length,)
                     for _, a, b, _, _, length in seen)
        want = sorted(tuple(sorted((self.NODES[i], self.NODES[j]))) + (length,)
                      for i, j, length in self.LINKS)
        self.assertEqual(got, want)

    def test_nodes_are_sorted_by_x(self):
        plain, _ = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS)
        rec = clarion_rd.parse_record(plain)
        xs = [n['x'] for n in rec['nodes']]
        self.assertEqual(xs, sorted(xs))

    def test_documented_header(self):
        plain, comp = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS)
        rec = clarion_rd.parse_record(plain)
        self.assertEqual(rec['code'], clarion_bg.parcel_code(1, 96 * 32 + 7, 898 * 32 + 9))
        self.assertEqual(struct.unpack_from('>H', plain, 52)[0], 0x017c)
        self.assertEqual(rec['pairs'][0], (0x1b, rec['unit_len']), 'the first unit starts at word 27')
        for off, ln in rec['pairs'][1:]:
            self.assertEqual((off, ln), (0xffffffff, 0))
        self.assertEqual(rec['classes'][0], (len(self.NODES), 2 * len(self.LINKS)))
        self.assertEqual(comp[:2], b'\x58\x85')
        self.assertEqual(zlib.decompress(comp), plain)

    def test_link_id_carries_the_northern_end(self):
        plain, _ = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS)
        rec = clarion_rd.parse_record(plain)
        for node in rec['nodes']:
            for e in node['entries']:
                other = rec['nodes'][e['other']]
                north = (node['y'], node['x']) > (other['y'], other['x'])
                self.assertEqual(bool(e['id'] & 0x2000), north, 'bit 0x2000 marks the larger y')

    def test_class_words(self):
        """The class word is class in the high byte then 0x90 | class, up to class 9."""
        for cls in range(1, 10):
            self.assertEqual(rd_encode.class_word(cls), cls << 8 | 0x90 | cls)
        self.assertEqual(rd_encode.class_word(0xa), 0xa8a)
        self.assertEqual(rd_encode.class_word(0xc), 0xc8c)
        self.assertEqual(rd_encode.class_word(8), 0x898, 'the common ordinary-road word')
        self.assertEqual(rd_encode.class_word(7), 0x797)

    def test_per_link_classes(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {},
                                                classes=['motorway', 'primary', 'service'])
        rec = clarion_rd.parse_record(plain)
        want = {rd_encode.class_word(fa_encode.layer_class(hw, 1)[1])
                for hw in ('motorway', 'primary', 'service')}
        got = {e['cls'] for n in rec['nodes'] for e in n['entries']}
        self.assertEqual(got, want)

    def test_parcel_edge_entry(self):
        """A node on a parcel edge gets a last entry with the side in the top bits."""
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS,
                                                {2: (0xc000, 17)})
        rec = clarion_rd.parse_record(plain)
        edges = [e for n in rec['nodes'] for e in n['entries'] if e['cls'] & 0x8000]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['cls'], 0xc000 | 0x0898, 'side 0xc is the bottom edge')
        self.assertEqual(edges[0]['other'], 17, 'the index is in the neighbouring parcel')

    def test_level2_ships_raw_in_two_parts(self):
        part_a, part_b = rd_encode.build_record_edges(2, 96, 898, 1, 2, self.NODES, self.LINKS,
                                                      {0: (0xc000, 7)})
        for part in (part_a, part_b):
            self.assertNotEqual(part[:2], b'\x58\x85', 'level-2 graph records are not compressed')
            self.assertEqual(struct.unpack_from('>H', part, 52)[0], 0x00fa, 'level-2 header tail')
        rec = clarion_rd.parse_record(part_a)
        self.assertIsNone(rec.get('error'))
        self.assertEqual(len(rec['nodes']), len(self.NODES))
        # part b is the connection table: nodes, 0xa100, then four words per node
        self.assertEqual(struct.unpack_from('>HH', part_b, 54), (len(self.NODES), 0xa100))

    def test_entry_count_ceiling(self):
        """The count shares its word with x, so 15 entries is the ceiling."""
        hub = [(10, 10)] + [(100 + 10 * i, 500) for i in range(16)]
        links = [(0, i, 100) for i in range(1, 17)]
        with self.assertRaises(AssertionError):
            rd_encode.build_record(1, 96, 898, 0, 0, hub, links)


class TestRoadShapes(unittest.TestCase):
    """fa_encode writes the record the renderer draws. Nothing in tools/ reads it back,
    so these check the bytes against the layout in GEOMETRY.md."""

    NODES = [(100, 200), (900, 240), (1500, 1800)]

    def chains(self, chain=False):
        return fa_encode.chains_from_links([(0, 1, 850), (1, 2, 1600)], {1: 10, 2: 11}, chain=chain)

    def unit_words(self, plain):
        w = struct.unpack_from('>22H', plain, 0)
        return w, struct.unpack_from('>%dH' % (2 * w[8]), plain, 4 * w[7])

    def test_level1_header(self):
        plain, comp = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.chains(),
                                             hws=['motorway', 'residential'])
        w, unit = self.unit_words(plain)
        self.assertEqual((w[0], w[1]), clarion_bg.parcel_code(1, 96 * 32 + 7, 898 * 32 + 9))
        self.assertEqual(w[2], 0x00fa)
        self.assertEqual((w[3], w[4]), (0x347a, 0x2aef))
        self.assertEqual((w[5], w[6]), (0, 0))
        self.assertEqual(w[7], 11, 'the geometry unit sits at dword 11')
        self.assertEqual(w[17], 11 + w[8], 'the tail table follows the geometry unit')
        self.assertEqual(w[18], 5, 'a 5-dword tail')
        self.assertEqual((w[19], w[20]), (0x8104, 0x017c))
        self.assertEqual(comp[:2], b'\x58\x85')
        self.assertEqual(zlib.decompress(comp), plain)

    def test_level2_header(self):
        plain, _ = fa_encode.build_record(2, 96, 898, 7, 9, self.NODES, self.chains(),
                                          hws=['motorway', 'residential'])
        w, _ = self.unit_words(plain)
        self.assertEqual(w[2], 0x00fa)
        self.assertEqual((w[3], w[4]), (0xffff, 0xffff))
        self.assertEqual((w[17], w[18]), (0xffff, 0), 'no tail unit at level 2')
        self.assertEqual((w[19], w[20]), (0, 0x00fa))

    def test_layer_pairs_point_at_the_shapes(self):
        """Twelve pairs and a pad word at level 1, eleven and no count word at level 2."""
        for level, first, npairs in ((1, 1, 12), (2, 0, 11)):
            plain, _ = fa_encode.build_record(level, 96, 898, 7, 9, self.NODES, self.chains(),
                                              hws=['motorway', 'residential'])
            w, unit = self.unit_words(plain)
            pairs = {i: (unit[first + 2 * i], unit[first + 1 + 2 * i]) for i in range(npairs)}
            used = {i: p for i, p in pairs.items() if p[0] != 0xffff}
            want_layers = {fa_encode.layer_class(hw, level)[0] for hw in ('motorway', 'residential')}
            self.assertEqual(set(used), want_layers, 'level %d' % level)
            for layer, (off, count) in used.items():
                self.assertEqual(count, 1, 'one shape per layer here')
                shape_len = unit[2 * off]
                self.assertGreater(shape_len, 0)
                self.assertLessEqual(2 * off + shape_len, len(unit), 'the shape fits the unit')
                self.assertEqual(unit[2 * off + 1] & 0x8000, 0x8000, 'word 1 is 0x8000 | points')

    def test_class_is_the_top_nibble_of_shape_word_2(self):
        for level in (1, 2):
            for hw in ('motorway', 'trunk', 'primary', 'residential', 'service'):
                plain, _ = fa_encode.build_record(level, 96, 898, 7, 9, self.NODES,
                                                  self.chains()[:1], hws=[hw])
                w, unit = self.unit_words(plain)
                first = 1 if level == 1 else 0
                npairs = 12 if level == 1 else 11
                layer, cls = fa_encode.layer_class(hw, level)
                off = unit[first + 2 * layer]
                self.assertNotEqual(off, 0xffff, '%s at level %d belongs to layer %d' % (hw, level, layer))
                self.assertEqual(unit[2 * off + 2] >> 12, cls)

    def test_point_count_matches_the_chain(self):
        for chain in (False, True):
            chains = self.chains(chain=chain)
            plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, chains,
                                              hws=['residential'] * len(chains))
            w, unit = self.unit_words(plain)
            off = unit[1 + 2 * 5]
            npts = unit[2 * off + 1] & 0x3fff
            want = 1 + sum(len(inner) + 1 for inner in (c[3] for c in chains[0]))
            self.assertEqual(npts, want)

    def test_chaining(self):
        self.assertEqual(len(self.chains(chain=False)), 2, 'one shape per link by default')
        self.assertEqual(len(self.chains(chain=True)), 2, 'two ways do not chain')
        same_way = fa_encode.chains_from_links([(0, 1, 850), (1, 2, 1600)], {1: 10, 2: 10}, chain=True)
        self.assertEqual(len(same_way), 1, 'consecutive links of one way form a chain')
        self.assertEqual([lid for lid, _, _, _ in same_way[0]], [1, 2])

    def test_names_section(self):
        labels = ['1=Hringvegur;2=R1', '1=Reykjanesbraut']
        items = [fa_encode.item_words([0, 0x8000 | 5], 1, 12)]
        sec, offs = fa_encode.names_section(labels, items)
        self.assertEqual(len(sec) % 4, 0)
        count, nlabels = struct.unpack_from('>HH', sec, 0)
        self.assertEqual((count, nlabels), (6, len(labels)))
        sub_off, nitems = struct.unpack_from('>HH', sec, 8)
        self.assertEqual(nitems, len(items))
        for text, off in offs.items():
            typ, flags, x, y, ln = struct.unpack_from('>5H', sec, 2 * off)
            self.assertEqual((typ, flags), (0x00ff, 0x2000), 'the road-label entry type')
            self.assertEqual((x, y), (0, 0))
            self.assertEqual(ln % 2, 0, 'the length includes the pad to an even byte count')
            self.assertEqual(sec[2 * off + 10:2 * off + 10 + ln].rstrip(b'\0'), text.encode())
        self.assertEqual(offs[labels[0]], 12, 'an entry right after the header sits at word 12')
        sub = struct.unpack_from('>HH', sec, 4 * sub_off)
        self.assertEqual(sub, (8, len(items)))

    def test_item_words(self):
        shape = [0, 0x8000 | 7]
        self.assertEqual(fa_encode.item_words(shape, 1)[2], 0x8000 | 2 * (7 - 2))
        level2 = fa_encode.item_words(shape, 2, name_off=12)
        self.assertEqual(level2[-3:], [1, 1, 12], 'k = 1 for a named level-2 shape')
        self.assertEqual(fa_encode.item_words(shape, 2, name_off=0xffff)[-3:], [1, 0, 0])

    def test_hws_must_match_the_chains(self):
        """build_record indexes its shapes by the hws list, so a mismatch is a caller bug.
        It is caught here rather than left to produce a short record."""
        with self.assertRaises(IndexError):
            fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.chains()[:1],
                                   hws=['motorway', 'residential'])


class TestRoadClasses(unittest.TestCase):
    """fa_encode.CLASSES against the table in ROAD-CLASSES.md."""

    DOC = {                        # highway: (L1 layer, L1 class, L2 class, L3 class)
        'motorway': (7, 1, 1, 3),
        'motorway_link': (7, 1, 1, 3),
        'trunk': (6, 4, 3, 4),
        'trunk_link': (6, 4, 3, 4),
        'primary': (6, 5, 4, 5),
        'primary_link': (6, 5, 4, 5),
        'secondary': (6, 5, 5, 6),
        'secondary_link': (6, 5, 5, 6),
        'tertiary': (5, 8, 7, 7),
        'unclassified': (5, 8, 7, 7),
        'residential': (5, 8, 7, 7),
        'living_street': (5, 8, 7, 7),
        'service': (4, 9, 7, 7),
        'track': (4, 9, 7, 7),
    }

    def test_table(self):
        self.assertEqual(set(fa_encode.CLASSES), set(self.DOC) | {'tertiary_link'})
        for hw, (layer, c1, c2, c3) in self.DOC.items():
            (l1, cls1), cls2, cls3 = fa_encode.CLASSES[hw]
            self.assertEqual((l1, cls1, cls2, cls3), (layer, c1, c2, c3), hw)

    def test_level1_bands(self):
        """Layer 7 takes classes 1-2, layer 6 takes 3-7, layer 5 class 8, layer 4 class 9."""
        for hw in self.DOC:
            layer, cls = fa_encode.layer_class(hw, 1)
            band = {7: (1, 2), 6: (3, 7), 5: (8, 8), 4: (9, 9)}[layer]
            self.assertTrue(band[0] <= cls <= band[1], (hw, layer, cls))

    def test_level2_layer_follows_the_class(self):
        for hw in self.DOC:
            layer, cls = fa_encode.layer_class(hw, 2)
            self.assertEqual(layer, 7 if cls in (1, 2) else 6, hw)

    def test_unknown_highway_falls_back(self):
        self.assertEqual(fa_encode.layer_class('bridleway', 1), fa_encode.layer_class('residential', 1))

    def test_layer_override(self):
        self.assertEqual(fa_encode.layer_class('motorway', 1, layer=6), (6, 1))


class TestRetarget(unittest.TestCase):
    """tile_retarget rewrites the six header bytes that separate the two databases."""

    def built_header(self):
        h = bytearray(b'\xff' * 0x400)
        for off, (was, now) in tile_retarget.DATABASES['2014'].items():
            h[off] = was
        return bytes(h)

    def test_six_bytes_and_no_others(self):
        raw = self.built_header()
        out = tile_retarget.convert(raw, '2014')
        changed = {i for i in range(len(raw)) if raw[i] != out[i]}
        self.assertEqual(changed, set(tile_retarget.DATABASES['2014']))
        self.assertEqual(len(changed), 6)
        for off, (was, now) in tile_retarget.DATABASES['2014'].items():
            self.assertEqual(out[off], now)

    def test_template_is_a_copy(self):
        raw = self.built_header()
        self.assertEqual(tile_retarget.convert(raw, tile_retarget.TEMPLATE), raw)
        self.assertEqual(tile_retarget.TEMPLATE, '2017', 'the build writes the 2017 strings')

    def test_already_converted_tile_fails_loudly(self):
        once = tile_retarget.convert(self.built_header(), '2014')
        with self.assertRaises(AssertionError):
            tile_retarget.convert(once, '2014')


class TestDirectoryIndex(unittest.TestCase):
    """make_drstr builds DRSTR001.DAT; this reads it back per FORMAT.md. The tree is
    made of empty files, so nothing off a card is needed."""

    DIR_REC, CHILD_REC = 0x28, 0x20

    def card_tree(self, root):
        p = Path(root)
        (p / 'MAPAL001').mkdir()
        (p / 'MAPAL001' / 'LEV3456R.DAT').write_bytes(b'\0' * 500)
        (p / 'MAPAL001' / 'B60R080R.DAT').write_bytes(b'\0' * 300)
        (p / 'VER_INFO').mkdir()
        (p / 'VER_INFO' / 'DB_INFO.TXT').write_bytes(b'GEURX\n0100\n')
        (p / 'REFER001').mkdir()
        (p / 'REFER001' / 'ADDRE001.DAT.cmp').write_bytes(b'\0' * 40)
        (p / 'REFER001' / 'ADDRE001.DAT.ind').write_bytes(struct.pack('<II', 1870080, 40) + b'\0' * 8)
        (p / 'SPEECH').mkdir()
        (p / 'SPEECH' / ('S' * 30)).write_bytes(b'')
        (p / 'MMAIN001.DAT').write_bytes(b'\0' * 4096)
        (p / 'DRSTR001.DAT').write_bytes(b'\0' * 10)
        return p

    def parse(self, blob):
        """(records, {directory name: [(child name, is_dir, size)]})."""
        ndirs = struct.unpack_from('<I', blob, 0x24)[0] // self.DIR_REC
        recs, children = [], {}
        for i in range(ndirs):
            name, own, parent, cnt = struct.unpack_from('<20sHHH', blob, i * self.DIR_REC)
            off = struct.unpack_from('<I', blob, i * self.DIR_REC + 0x24)[0]
            zeros = blob[i * self.DIR_REC + 0x1a:i * self.DIR_REC + 0x24]
            name = name.rstrip(b'\0').decode()
            recs.append(dict(name=name, id=own, parent=parent, count=cnt, off=off, zeros=zeros))
            rows = []
            for k in range(cnt):
                cn, z, kind, z2, size = struct.unpack_from('<20sHHII', blob, off + k * self.CHILD_REC)
                rows.append((cn.rstrip(b'\0').decode(), kind == 2, size, z, z2))
            children[name] = rows
        return recs, children

    def test_tree_linkage(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            recs, children = self.parse(make_drstr.build(root))
        self.assertEqual([r['name'] for r in recs][0], '/')
        self.assertEqual(recs[0]['id'], 1)
        self.assertEqual(recs[0]['parent'], 1, 'the root is its own parent')
        for i, r in enumerate(recs):
            self.assertEqual(r['id'], i + 1, 'ids are 1-based in record order')
            self.assertEqual(r['zeros'], b'\0' * 10)
        by_name = {r['name']: r for r in recs}
        self.assertEqual(set(by_name), {'/', 'VER_INFO', 'MAPAL001', 'REFER001'})
        for name in ('VER_INFO', 'MAPAL001', 'REFER001'):
            self.assertEqual(by_name[name]['parent'], 1, name)
            self.assertEqual(by_name[name]['count'], len(children[name]))

    def test_child_blocks_follow_the_records_in_order(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            blob = make_drstr.build(root)
        recs, children = self.parse(blob)
        pos = len(recs) * self.DIR_REC
        for r in recs:
            self.assertEqual(r['off'], pos)
            pos += r['count'] * self.CHILD_REC
        self.assertEqual(pos, len(blob), 'the file ends with the last child block')

    def test_speech_and_the_index_itself_are_omitted(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            recs, children = self.parse(make_drstr.build(root))
        names = {n for rows in children.values() for n, _, _, _, _ in rows}
        self.assertNotIn('SPEECH', names, 'its filenames do not fit the 20-byte field')
        self.assertNotIn('DRSTR001.DAT', names, 'a file cannot record its own size')

    def test_ver_info_is_hoisted(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            recs, children = self.parse(make_drstr.build(root))
        self.assertEqual(children['/'][0][0], 'VER_INFO')

    def test_compressed_files_are_listed_logically(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            recs, children = self.parse(make_drstr.build(root))
        rows = children['REFER001']
        self.assertEqual([(n, d) for n, d, _, _, _ in rows], [('ADDRE001.DAT', False)])
        self.assertEqual(rows[0][2], 1870080, 'the uncompressed size out of the .ind header')

    def test_directory_size_sums_what_is_listed(self):
        with tempfile.TemporaryDirectory() as root:
            self.card_tree(root)
            recs, children = self.parse(make_drstr.build(root))
        sizes = {n: s for n, d, s, _, _ in children['/']}
        self.assertEqual(sizes['MAPAL001'], 800)
        self.assertEqual(sizes['REFER001'], 1870080, 'not the 48 bytes of .cmp plus .ind on disk')
        self.assertEqual(sizes['MMAIN001.DAT'], 4096)

    def test_directory_size_truncates_to_32_bits(self):
        """MAPAL001 really holds 5,162,906,624 bytes and the field reads 867,939,328. The
        .ind header declares the size, so this needs no large file."""
        with tempfile.TemporaryDirectory() as root:
            p = Path(root, 'MAPAL001')
            p.mkdir()
            for n in ('A', 'B'):
                (p / (n + '.DAT.cmp')).write_bytes(b'\0' * 8)
                (p / (n + '.DAT.ind')).write_bytes(struct.pack('<II', 3 << 30, 8) + b'\0' * 8)
            self.assertEqual(make_drstr.tree_size(p), 2 * (3 << 30))
            recs, children = self.parse(make_drstr.build(root))
        self.assertEqual(children['/'][0][2], (2 * (3 << 30)) & 0xffffffff)

    def test_long_name_is_refused(self):
        with self.assertRaises(ValueError):
            make_drstr.name_field('A' * 21)

    def test_name_field_is_nul_padded(self):
        self.assertEqual(make_drstr.name_field('MAPAL001'), b'MAPAL001' + b'\0' * 12)


class TestProfiles(unittest.TestCase):
    """clarion_profile reads a region's TOML. tomllib is standard library."""

    def test_defaults(self):
        P = clarion_profile.default()
        self.assertEqual(P.bounds, clarion_profile.EUROPE_BOUNDS)
        self.assertEqual(P.cells, [])
        self.assertFalse(P.shift)
        self.assertEqual(P.west_meshes, 0)
        self.assertEqual(P.land, clarion_profile.LAND)
        self.assertEqual(P.l2_classes, clarion_profile.L2CLASSES_WIDE)

    def test_europe_box_and_its_mesh_origin(self):
        lat_max, lat_min, lon_min, lon_max = clarion_profile.EUROPE_BOUNDS
        self.assertEqual((lat_min / 60, lon_min / 60), (21 + 1 / 3, -20.0))
        self.assertEqual(clarion_profile.mesh_origin(clarion_profile.EUROPE_BOUNDS), (32, 904))
        self.assertEqual(clarion_bg.mesh_to_lon(904), -20)

    def test_iceland(self):
        P = clarion_profile.load('iceland')
        self.assertEqual(P.cells, ['B58R078R', 'B58R080R', 'B60R078R', 'B60R080R'])
        self.assertEqual(P.bounds, (4800, 1280, -1680, 3600), 'the grid shifted 8 degrees west')
        self.assertTrue(P.shift)
        self.assertEqual(P.west_meshes, 8)
        self.assertEqual(P.mesh_origin, (32, 896))
        self.assertEqual(clarion_bg.mesh_to_lon(896), -28, 'the shifted west edge')
        for cell in P.cells:
            y0, x0 = clarion_bg.tile_cell(cell)
            self.assertTrue(896 <= x0 < 896 + 88)
        for name in ('v2014', 'v2017'):
            self.assertIn(name, P.targets)

    def test_missing_profile(self):
        with self.assertRaises(SystemExit):
            clarion_profile.load('no-such-region')

    def test_l2_class_spellings(self):
        self.assertEqual(clarion_profile.l2_classes('narrow'), clarion_profile.L2CLASSES_NARROW)
        self.assertEqual(clarion_profile.l2_classes('wide'), clarion_profile.L2CLASSES_WIDE)
        self.assertEqual(clarion_profile.l2_classes('motorway,trunk'), {'motorway', 'trunk'})
        self.assertEqual(clarion_profile.l2_classes(['service']), {'service'})

    def test_take_arg(self):
        argv = ['tool', '--profile', 'iceland', '--out', 'x']
        P = clarion_profile.take_arg(argv)
        self.assertEqual(argv, ['tool', '--out', 'x'])
        self.assertEqual(P.name, 'iceland')


@unittest.skipIf(bg_encode is None, 'needs shapely')
class TestAgainstBgEncode(unittest.TestCase):
    """bg_encode is the encoder the chain runs. It needs shapely to import, so the cases
    above build their records and directories with local copies of its two pure helpers.
    These check the copies have not drifted."""

    def test_parcel_record_matches(self):
        sea = [dict(type=clarion_bg.LEVELS[1]['seatype'], ref=0xffff, pts=FULL_PARCEL_SEA)]
        theirs, _ = bg_encode.build_parcel_record(1, 96, 898, 7, 9, sea, [], [])
        mine, _ = background_record(1, 96, 898, 7, 9, [FULL_PARCEL_SEA])
        self.assertEqual(theirs, mine)

    def test_directory_matches(self):
        recs = {7: {9: background_record(1, 96, 898, 7, 9, [FULL_PARCEL_SEA])}}
        theirs = bg_encode.make_dir(1, 96, 898, recs)
        mine = background_dir(1, 96, 898, recs)
        for k in ('kind', 'ab', 'y', 'x', 'typ', 'body', 'nslots', 'box', 'empty_bodies'):
            self.assertEqual(theirs[k], mine[k], k)
        self.assertEqual(theirs['slots'].keys(), mine['slots'].keys())
        for row in theirs['slots']:
            self.assertEqual(theirs['slots'][row]['body'], mine['slots'][row]['body'])

    def test_quant_matches(self):
        for v in (0.0, 0.25, 0.5, 0.9999, 1.0):
            self.assertEqual(bg_encode.quant(v, 0.0, 1.0), quant(v, 0.0, 1.0))

    def test_densify_fits_int8_deltas(self):
        pts = bg_encode.densify([(0, 0), (0, 2047), (2047, 2047)])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            self.assertTrue(-128 <= x1 - x0 <= 127 and -128 <= y1 - y0 <= 127)

    def test_vertex_budget(self):
        self.assertEqual(bg_encode.MAX_VERTS, 600, 'the unit stops filling longer shapes')

    def test_label_spec(self):
        got = bg_encode.parse_labels('0x4089:0x2800:-22.5,64.2:ATLANTSHAF')
        self.assertEqual(got, [(0x4089, 0x2800, -22.5, 64.2, 'ATLANTSHAF', 0x35d8)])


if __name__ == '__main__':
    unittest.main()
