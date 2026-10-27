#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Roundtrip checks for the readers and encoders in tools/: decode(encode(x)) gives x back and the bytes match the docs/ layout. Built in code, so no card or OSM extract is needed; shapely-only cases skip without it."""
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
import extract_landuse
import fa_encode
import make_drstr
import rd_build
import rd_check
import rd_encode
import tile_grid
import tile_retarget

try:
    import bg_encode
except ImportError:                      # shapely absent
    bg_encode = None

UNITS = clarion_bg.UNITS                 # 2048 parcel units per side


# vertices are int8 deltas, so a long edge is walked in steps of at most 127 units (bg_encode.densify, without pulling shapely in here)

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
    """A section-1 directory for write_tile; mirrors bg_encode.make_dir, which needs shapely to import."""
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
    """A 0x1000 header: strings, level table at 0x1ac, box table at 0x2ac, section table at 0x800."""
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
    """clarion_bg.parcel_code against the docs' formulas, over the European box where column >> 8 = 1 feeds w0's top bit."""

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
        self.assertEqual(clarion_bg.parcel_code(1, 88 * 32, 918 * 32 + 24), (0xac0c, 0xcb6c))

    def test_absolute_not_directory_local(self):
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
        g = dict(kind=0x80, shapes=[dict(type=0x8012, ref=0xffff, pts=closed_ring([(0, 0), (0, 300), (300, 300)]))],
                 more=[dict(kind=0x40, shapes=[dict(type=0x8007, ref=0xffff, pts=[(10, 10), (20, 20)])])])
        raw = self.roundtrip(g)
        self.assertEqual(struct.unpack_from('>HH', raw, 0), (2, 2 + 2 * 2 - 1))

    def test_vertex_delta_limit(self):
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
        plain, _ = background_record(2, 96, 896, 3, 5)
        self.assertEqual(len(plain), 44)
        w = struct.unpack_from('>22H', plain, 0)
        self.assertEqual((w[9], w[10]), (0xffff, 0))
        rec = clarion_bg.parse_record(plain)
        self.assertEqual(rec['nlayers'], 0)
        self.assertEqual(clarion_bg.build_record(rec), plain)

    def test_zlib_stream_shape(self):
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
            # half a parcel unit at level 1 is well under a metre; real coastline tops out at 1.3 m (GEOMETRY.md)
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


@unittest.skipIf(bg_encode is None, 'needs shapely')
class TestSeaPolygonHoles(unittest.TestCase):
    """An island that doesn't touch a parcel edge is a hole in the sea polygon; bg_encode.no_holes cuts it out so the island stays uncovered."""

    def test_no_holes_uncovers_the_hole(self):
        from shapely.geometry import Point, Polygon
        outer = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
        hole = Polygon([(4, 4), (4, 6), (6, 6), (6, 4)])
        water = outer.difference(hole)
        pieces = bg_encode.no_holes(water)
        self.assertTrue(all(len(p.interiors) == 0 for p in pieces), 'no returned piece keeps a hole')
        self.assertTrue(all(not p.contains(Point(5, 5)) for p in pieces), 'the island stays uncovered')
        self.assertAlmostEqual(sum(p.area for p in pieces), water.area, places=6)

    def test_island_inside_a_sea_parcel_is_not_filled(self):
        from shapely.geometry import LineString, Point, Polygon, box
        from shapely.strtree import STRtree

        import bg_tiles
        level, y0, x0, row, col = 1, 96, 898, 7, 9
        lon0, lat0, w, h = clarion_bg.parcel_geo(level, y0, x0, row, col)
        rect = box(lon0, lat0, lon0 + w, lat0 + h)
        island = box(lon0 + w * 0.4, lat0 + h * 0.4, lon0 + w * 0.6, lat0 + h * 0.6)
        coast = LineString(list(island.exterior.coords))
        recs = bg_tiles.encode_level_fast(level, y0, x0, island, [coast], STRtree([coast]), rect)
        plain, _ = recs[row][col]
        r = clarion_bg.parse_record(plain)
        shapes = r['layers'][1]['shapes']
        self.assertGreater(len(shapes), 1, 'the hole forces the sea into more than one ring')
        cx = bg_encode.quant(lon0 + w * 0.5, lon0, w)
        cy = bg_encode.quant(lat0 + h * 0.5, lat0, h)
        for s in shapes:
            self.assertFalse(Polygon(s['pts']).contains(Point(cx, cy)), 'the island centre stays sea-free')


class TestRoadGraph(unittest.TestCase):
    """rd_encode writes a section-2 record, clarion_rd reads it back."""

    NODES = [(100, 200), (900, 240), (1500, 1800), (300, 1000)]
    LINKS = [(0, 1, 850), (1, 2, 1600), (0, 3, 700)]

    def test_nodes_and_links_roundtrip(self):
        plain, comp = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS, country=352)
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
        plain, _ = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS, country=352)
        rec = clarion_rd.parse_record(plain)
        xs = [n['x'] for n in rec['nodes']]
        self.assertEqual(xs, sorted(xs))

    def test_documented_header(self):
        plain, comp = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.LINKS, country=352)
        rec = clarion_rd.parse_record(plain)
        self.assertEqual(rec['code'], clarion_bg.parcel_code(1, 96 * 32 + 7, 898 * 32 + 9))
        self.assertEqual(struct.unpack_from('>H', plain, 52)[0], 352, 'word 26 is the country')
        self.assertEqual(rec['pairs'][0], (0x1b, rec['unit_len']), 'the first unit starts at word 27')
        for off, ln in rec['pairs'][1:]:
            self.assertEqual((off, ln), (0xffffffff, 0))
        self.assertEqual(rec['classes'][3], (len(self.NODES), 2 * len(self.LINKS)),
                         'class 8 nodes and their link ends sit in the u10 pair')
        self.assertEqual(comp[:2], b'\x58\x85')
        self.assertEqual(zlib.decompress(comp), plain)

    def test_link_id_flag_marks_the_drawn_last_point(self):
        # link 2 runs from node 2 south-west to node 3, so its drawn end is the lower one
        links = self.LINKS + [(2, 3, 1200)]
        plain, _ = rd_encode.build_record(1, 96, 898, 7, 9, self.NODES, links, country=352)
        rec = clarion_rd.parse_record(plain)
        at = {(n['x'], n['y']): n for n in rec['nodes']}
        for i, j, _ in links:
            for node, flag in ((self.NODES[i], 0), (self.NODES[j], 0x2000)):
                e = [e for e in at[node]['entries'] if (rec['nodes'][e['other']]['x'], rec['nodes'][e['other']]['y'])
                     == (self.NODES[j] if flag == 0 else self.NODES[i])]
                self.assertEqual([x['id'] & 0x2000 for x in e], [flag], 'bit 0x2000 sits at the j end only')

    def test_link_ids_are_given(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {}, country=352,
                                                classes=['trunk', 'trunk', 'residential'], ids=[1, 2, 1])
        rec = clarion_rd.parse_record(plain)
        got = sorted({(e['cls'] & 0xf, e['id'] & 0x1fff) for n in rec['nodes'] for e in n['entries']})
        self.assertEqual(got, [(4, 1), (4, 2), (8, 1)], 'ids restart per class; the class names the group')

    def test_class_words(self):
        for cls in range(1, 10):
            self.assertEqual(rd_encode.class_word(cls), cls << 8 | 0x90 | cls)
        self.assertEqual(rd_encode.class_word(0xa), 0xa8a)
        self.assertEqual(rd_encode.class_word(0xc), 0xc8c)
        self.assertEqual(rd_encode.class_word(8), 0x898, 'the common ordinary-road word')
        self.assertEqual(rd_encode.class_word(7), 0x797)

    def test_per_link_classes(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {},
                                                classes=['motorway', 'primary', 'service'], country=352)
        rec = clarion_rd.parse_record(plain)
        want = {rd_encode.class_word(fa_encode.layer_class(hw, 1)[1])
                for hw in ('motorway', 'primary', 'service')}
        got = {e['cls'] & 0xff | (e['cls'] & 0xf) << 8 for n in rec['nodes'] for e in n['entries']}
        self.assertEqual(got, want, 'the low byte is the link class word')

    def test_oneway_bits(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {2: (0xc000, 17)},
                                                country=352, dirs=[1, -1, 0])
        self.assertEqual(rd_check.check(plain, 1, 352), ([], []))
        self.assertEqual(rd_check.check.oneway, 2)
        ow, bad = rd_check.oneway_links(clarion_rd.parse_record(plain))
        self.assertEqual((sorted((i, j) for i, j, _ in ow), bad), ([(0, 2), (3, 2)], []))
        self.assertEqual(rd_encode.oneway_bits(-1), (0x8000, 0x4000))

    def test_oneway_regulations(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {},
                                                country=352, dirs=[1, -1, 0])
        self.assertEqual(rd_check.check(plain, 1, 352), ([], []))
        self.assertEqual((rd_check.check.oneway, rd_check.check.no_entry, rd_check.check.beside, rd_check.check.barred),
                         (2, 2, 3, 3))
        rec = clarion_rd.parse_record(plain)
        for nd in rec['nodes']:
            ents = nd['entries']
            leaving = [k << 12 | 0xff for k, e in enumerate(ents) if e['w5'] & 0xc000 == 0x4000]
            for k, e in enumerate(ents):
                regs = e['extra'][:(e['w5'] >> 6) & 0x3f]
                self.assertEqual(regs, [rd_check.NO_ENTRY] if e['w5'] & 0xc000 == 0x8000 else leaving)
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {},
                                                country=352, dirs=[1, -1, 0], oneway_regs=False)
        rd_check.check(plain, 1, 352)
        self.assertEqual((rd_check.check.oneway, rd_check.check.no_entry, rd_check.check.beside, rd_check.check.barred),
                         (2, 0, 3, 0))

    def test_oneway_regulations_with_turns(self):
        self.assertEqual(rd_encode.regulation_order([0x00ff, 0x20ff, 0x10ff], 1), [0x20ff, 0x00ff, 0x10ff])
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {},
                                                country=352, dirs=[1, 0, 0], regs=[(0, 2, 0), (0, 2, 0), (1, 1, 0)])
        self.assertEqual(rd_encode.build_record_edges.regs_written, 1)
        self.assertEqual(rd_check.check(plain, 1, 352), ([], []))
        rec = clarion_rd.parse_record(plain)
        words = {(nd['x'], e['id'] & 0x1fff): e['extra'][:(e['w5'] >> 6) & 0x3f] for nd in rec['nodes'] for e in nd['entries']}
        self.assertEqual(words[(100, 1)], [0x10ff, 0x00ff], 'link 1 leaves node 0: the turn from entry 1, then itself')
        self.assertEqual(words[(900, 1)], [rd_check.NO_ENTRY])

    def test_parcel_edge_entry(self):
        plain, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS,
                                                {2: (0xc000, 17)}, country=352)
        rec = clarion_rd.parse_record(plain)
        edges = [e for n in rec['nodes'] for e in n['entries'] if e['cls'] & 0x8000]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['cls'], 0xc000 | 0x0898, 'side 0xc is the bottom edge')
        self.assertEqual(edges[0]['other'], 17, 'the index is in the neighbouring parcel')

    def test_level2_ships_raw_in_two_parts(self):
        part_a, part_b = rd_encode.build_record_edges(2, 96, 898, 1, 2, self.NODES, self.LINKS,
                                                      {0: (0xc000, 7)}, country=352)
        for part in (part_a, part_b):
            self.assertNotEqual(part[:2], b'\x58\x85', 'level-2 graph records are not compressed')
            self.assertEqual(struct.unpack_from('>H', part, 52)[0], 352, 'both parts carry the country')
        rec = clarion_rd.parse_record(part_a)
        self.assertIsNone(rec.get('error'))
        self.assertEqual(len(rec['nodes']), len(self.NODES))
        # part b is the connection table: nodes, 0xa100, then four words per node
        self.assertEqual(struct.unpack_from('>HH', part_b, 54), (len(self.NODES), 0xa100))

    def test_entry_count_ceiling(self):
        hub = [(10, 10)] + [(100 + 10 * i, 500) for i in range(16)]
        links = [(0, i, 100) for i in range(1, 17)]
        with self.assertRaises(AssertionError):
            rd_encode.build_record(1, 96, 898, 0, 0, hub, links, country=352)


class TestRoadHeader(unittest.TestCase):
    """The section-2 header rule: one (nodes, link ends) pair per node-class group, totals without edge connections, country at word 26."""

    NODES = [(100, 200), (900, 240), (1500, 1800), (300, 1000)]
    LINKS = [(0, 1, 850), (1, 2, 1600), (0, 3, 700)]
    HW = ['motorway', 'primary', 'service']          # classes 1, 5 and 9 at level 1

    def build(self, edges=None, level=1, hw=HW):
        return rd_encode.build_record_edges(level, 96, 898, 7, 9, self.NODES, self.LINKS, edges or {},
                                            classes=hw, country=352)

    def test_pairs_follow_the_node_class(self):
        plain, _ = self.build()
        rec = clarion_rd.parse_record(plain)
        # nodes 0 and 1 meet the motorway (class 1), node 2 is class 5, node 3 class 9
        want = [(0, 0)] * 11
        want[1], want[2], want[4] = (2, 4), (1, 1), (1, 1)
        self.assertEqual(rec['classes'], want, 'u6 for classes 0-2, u8 for 3-7, u12 for 9')
        self.assertEqual(rd_check.check(plain), ([], []))

    def test_slots(self):
        self.assertEqual([rd_encode.pair_slot(c) for c in (0, 1, 2, 3, 7, 8, 9, 10, 12)],
                         [1, 1, 1, 2, 2, 3, 4, 10, 10])
        self.assertEqual([rd_check.slot(c) for c in range(16)], [rd_encode.pair_slot(c) for c in range(16)])

    def test_edge_connections_are_not_link_ends(self):
        plain, _ = self.build({2: (0xc000, 17), 3: (0x8000, 4)})
        rec = clarion_rd.parse_record(plain)
        self.assertEqual(rec['nlinks'], 2 * len(self.LINKS))
        self.assertEqual(sum(l for _, l in rec['classes']), 2 * len(self.LINKS))
        self.assertEqual(rd_check.check(plain), ([], []))

    def test_class_words_name_the_far_node(self):
        plain, _ = self.build({2: (0xc000, 17)})
        rec = clarion_rd.parse_record(plain)
        for nd in rec['nodes']:
            real = [e for e in nd['entries'] if not e['cls'] & 0x8000]
            self.assertEqual(nd['ncls'], min(e['cls'] & 0xf for e in real), 'y carries the lowest link class')
            for e in nd['entries']:
                if e['cls'] & 0x8000:
                    self.assertEqual(e['cls'], 0xc000 | nd['ncls'] << 8 | real[0]['cls'] & 0xff)
                else:
                    self.assertEqual(e['cls'] >> 8, rec['nodes'][e['other']]['ncls'])

    def test_level2_both_parts_carry_the_country(self):
        part_a, part_b = self.build({0: (0xc000, 7)}, level=2)
        self.assertEqual(rd_check.check(part_a + part_b, level=2, country=352), ([], []))
        for part in (part_a, part_b):
            self.assertEqual(struct.unpack_from('>H', part, 52)[0], 352)

    def test_country_comes_from_the_profile(self):
        self.assertEqual(clarion_profile.load(str(ROOT / 'profiles' / 'iceland.toml')).country, 352)
        with self.assertRaises(AssertionError):
            rd_encode.build_record_edges(1, 96, 898, 7, 9, self.NODES, self.LINKS, {})
        plain, _ = self.build()
        self.assertEqual(rd_check.check(plain, country=380)[0], ['countries [352], want [380]'])

    def test_old_layout_is_refused(self):
        """What rd_encode used to write: the whole record in u10, edges counted."""
        plain, _ = self.build({2: (0xc000, 17)}, hw=['primary'] * 3)
        w = list(struct.unpack('>%dH' % (len(plain) // 2), plain))
        u = 27
        nn, nl = w[u + 1], w[u + 3] + 1
        w[u + 3] = nl
        w[u + 4:u + 26] = [0, 0, 0, 0, 0, 0, nn, nl] + [0] * 14
        hp, _ = rd_check.check(struct.pack('>%dH' % len(w), *w))
        self.assertEqual(len(hp), 2, hp)
        self.assertIn('totals (4, 7), body gives (4, 6)', hp)

    def test_entry_extra_words(self):
        """w5 bits 6-11 count regulation words, bits 0-3 further words after them; a 4-bit-only reader loses its place on dense tiles."""
        regs = list(range(0x1001, 0x1001 + 20))
        unit = rd_encode.unit_head([8, 8], [1, 1]) + [
            1 << 12 | 100, 0x8000 | 200, 0, 1, 0, 20 << 6 | 2, 1, 0x898, 0x8010, 40] + regs + [0x20ff, 0x0410] + [
            1 << 12 | 300, 0x8000 | 400, 0, 0, 0, 0, 0x2001, 0x898, 0x8010, 40]
        head, _ = rd_encode.record_head(1, 96, 898, 7, 9, [(0x1b, len(unit))], 352)
        rec = clarion_rd.parse_record(head + struct.pack('>%dH' % len(unit), *unit))
        self.assertEqual(rec['consumed'], rec['unit_len'])
        self.assertEqual(len(rec['nodes']), 2)
        self.assertEqual(rec['nodes'][0]['entries'][0]['extra'], regs + [0x20ff, 0x0410])
        self.assertEqual(rd_check.check(head + struct.pack('>%dH' % len(unit), *unit)), ([], []))

    def test_simplify_keeps_junctions(self):
        line = [(-21.75 + i * 0.0001, 64.12) for i in range(5)]
        self.assertEqual(rd_build.simplify(line, 2), [line[0], line[-1]])
        self.assertEqual(rd_build.simplify(line, 2, {line[2]}), [line[0], line[2], line[-1]])

    def test_components_join_across_parcel_edges(self):
        nodes, links = [(100, 100), (2047, 100), (500, 900), (600, 900)], [(0, 1, 50), (2, 3, 10)]
        west, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, nodes, links, {1: (0xa000, 0)}, country=352)
        east, _ = rd_encode.build_record_edges(1, 96, 898, 7, 10, [(0, 100), (900, 100)], [(0, 1, 50)],
                                               {0: (0xe000, 3)}, country=352)
        got = rd_check.pieces([west, east])
        self.assertEqual(sorted(n for _, n in got), [2, 4])
        self.assertEqual(sorted(len(l) for l, _ in got), [1, 2])

    def test_edge_entries_answer_each_other(self):
        nodes, links = [(100, 100), (2047, 100), (500, 900), (600, 900)], [(0, 1, 50), (2, 3, 10)]
        west, _ = rd_encode.build_record_edges(1, 96, 898, 7, 9, nodes, links, {1: (0xa000, 0)}, country=352)
        east, _ = rd_encode.build_record_edges(1, 96, 898, 7, 10, [(0, 100), (900, 100)], [(0, 1, 50)],
                                               {0: (0xe000, 3)}, country=352)
        self.assertEqual(rd_check.edge_joins([west, east]), (2, []))
        wrong, _ = rd_encode.build_record_edges(1, 96, 898, 7, 10, [(0, 100), (900, 100)], [(0, 1, 50)],
                                                {0: (0xe000, 1)}, country=352)
        self.assertEqual(len(rd_check.edge_joins([west, wrong])[1]), 2)
        self.assertEqual(rd_check.edge_joins([west])[1][0][5], 'neighbour parcel not read')

    def test_two_country_records(self):
        """A parcel a border runs through lists both codes and starts unit 0 a word later."""
        unit = rd_encode.unit_head([8, 8], [1, 1]) + [
            1 << 12 | 100, 0x8000 | 200, 0, 1, 0, 0, 1, 0x898, 0x8010, 40,
            1 << 12 | 300, 0x8000 | 400, 0, 0, 0, 0, 0x2001, 0x898, 0x8010, 40]
        head, _ = rd_encode.record_head(1, 96, 898, 7, 9, [(28, len(unit))], 578)
        rec = clarion_rd.parse_record(head + struct.pack('>H', 752) + struct.pack('>%dH' % len(unit), *unit))
        self.assertEqual(rec['countries'], [578, 752])
        self.assertEqual(rd_check.check(head + struct.pack('>H', 752) + struct.pack('>%dH' % len(unit), *unit)), ([], []))


class TestRoadShapes(unittest.TestCase):
    """fa_encode writes the record the renderer draws, checked here against GEOMETRY.md's layout."""

    NODES = [(100, 200), (900, 240), (1500, 1800)]
    CHAIN = [(1, 0, 1, [(300, 210), (600, 225)]), (2, 1, 2, [(1100, 900)])]

    def chains(self, chain=False):
        return fa_encode.chains_from_links([(0, 1, 850), (1, 2, 1600)], {1: 10, 2: 11}, chain=chain)

    def unit_words(self, plain):
        w = struct.unpack_from('>22H', plain, 0)
        return w, struct.unpack_from('>%dH' % (2 * w[8]), plain, 4 * w[7])

    def shape_points(self, shape, chain, fresh=None):
        """(role, x, y) per point of a level-1 shape, walked beside the chain that produced it; fresh marks which links open with a style word."""
        if fresh is None: fresh = [n == 0 for n in range(len(chain))]
        p = 7                                       # the length word, 0x8000 | points, five header words
        out = [('start', shape[p], shape[p + 1])]; p += 2
        for n, (_, _, _, inner) in enumerate(chain):
            p += 3 if fresh[n] else 2                # the link header
            npts = len(inner) + 1
            for k in range(npts):
                if k < npts - 1: role = 'first-of-link' if k == 0 else 'inner'
                else: role = 'last' if n == len(chain) - 1 else 'linkend'
                out.append((role, shape[p], shape[p + 1])); p += 2
        self.assertEqual(shape[p], 0x0000, 'the trailer word ends the shape')
        self.assertEqual((shape[0], len(shape)), (p + 1, p + 1), 'the length word counts itself')
        self.assertEqual(shape[1] & 0x3fff, len(out))
        return out

    def test_level1_header(self):
        plain, comp = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.chains(),
                                             hws=['motorway', 'residential'], country=352)
        w, unit = self.unit_words(plain)
        self.assertEqual((w[0], w[1]), clarion_bg.parcel_code(1, 96 * 32 + 7, 898 * 32 + 9))
        self.assertEqual(w[2], 0x00fa)
        self.assertEqual((w[3], w[4]), (0x347a, 0x2aef))
        self.assertEqual((w[5], w[6]), (0, 0))
        self.assertEqual(w[7], 11, 'the geometry unit sits at dword 11')
        self.assertEqual(w[17], 11 + w[8], 'the tail table follows the geometry unit')
        self.assertEqual(w[18], 5, 'a 5-dword tail')
        self.assertEqual((w[19], w[20]), (0x8104, 352), 'word 20 is the country')
        self.assertEqual(comp[:2], b'\x58\x85')
        self.assertEqual(zlib.decompress(comp), plain)

    def test_level2_header(self):
        plain, _ = fa_encode.build_record(2, 96, 898, 7, 9, self.NODES, self.chains(),
                                          hws=['motorway', 'residential'], country=352)
        w, _ = self.unit_words(plain)
        self.assertEqual(w[2], 0x00fa)
        self.assertEqual((w[3], w[4]), (0xffff, 0xffff))
        self.assertEqual((w[17], w[18]), (0xffff, 0), 'no tail unit at level 2')
        self.assertEqual((w[19], w[20]), (0, 352), 'word 20 is the country')

    def test_layer_pairs_point_at_the_shapes(self):
        for level, first, npairs in ((1, 1, 12), (2, 0, 11)):
            plain, _ = fa_encode.build_record(level, 96, 898, 7, 9, self.NODES, self.chains(),
                                              hws=['motorway', 'residential'], country=352)
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
                                                  self.chains()[:1], hws=[hw], country=352)
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
                                              hws=['residential'] * len(chains), country=352)
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

    def test_a_point_that_does_not_end_a_link_carries_0x0800(self):
        """The firmware reads a clear bit 11 as the link ending there, and reads the next two words as a link header."""
        for role, x, _ in self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES), self.CHAIN):
            want = 0 if role in ('linkend', 'last') else 0x0800
            self.assertEqual(x & 0x0800, want, 'a %s point' % role)

    def test_flags_stay_out_of_the_coordinate(self):
        pts = self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES), self.CHAIN)
        want = ([self.NODES[0]] + self.CHAIN[0][3] + [self.NODES[1]]
                + self.CHAIN[1][3] + [self.NODES[2]])
        self.assertEqual([(x & 0x07ff, y & 0x07ff) for _, x, y in pts], want)

    def test_a_link_end_inside_the_chain_marks_itself_on_y(self):
        pts = self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES), self.CHAIN)
        _, x, y = next(p for p in pts if p[0] == 'linkend')
        self.assertEqual(x & 0x1000, 0, 'a link header follows this point')
        self.assertEqual(y & 0xf800, 0x0800)
        _, x, y = pts[-1]
        self.assertEqual(x & 0x1000, 0x1000, 'nothing follows the shape')
        self.assertEqual(y & 0xf800, 0, 'the last point has a plain y')

    def test_the_start_node_and_the_first_link(self):
        pts = self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES), self.CHAIN)
        self.assertEqual(pts[0][1] & 0xf800, 0x0800, 'the start x')
        self.assertEqual(pts[0][2] & 0xf800, 0x2800, 'the start y')
        self.assertEqual([i for i, (_, x, _) in enumerate(pts) if x & 0x4000], [1, 4],
                         '0x4000 on the first point of each link')
        self.assertEqual([i for i, (_, x, _) in enumerate(pts) if x & 0x2000], [1],
                         '0x2000 marks a link opened by a style word; with no styles given '
                         'here, only the first link is one')

    def test_a_restyled_later_link_carries_0x2000(self):
        styles = [fa_encode.STYLE, fa_encode.style_word(30)]
        fresh = fa_encode.restyled(styles)
        self.assertEqual(fresh, [True, True], 'both links open with their own style word')
        pts = self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES, styles=styles),
                                self.CHAIN, fresh=fresh)
        self.assertEqual([i for i, (_, x, _) in enumerate(pts) if x & 0x4000], [1, 4])
        self.assertEqual([i for i, (_, x, _) in enumerate(pts) if x & 0x2000], [1, 4],
                         'the second link is restyled too, so it carries 0x2000 as well')

    def test_oneway_bits_mark_every_y_of_the_link(self):
        for dirs, want in (([1, -1], [0x4000] * 4 + [0x8000] * 2),
                           ([0, 0], [0] * 6),
                           ([-1, 1], [0x8000] * 4 + [0x4000] * 2)):
            pts = self.shape_points(fa_encode.shape_words(self.CHAIN, self.NODES, dirs=dirs), self.CHAIN)
            self.assertEqual([y & 0xc000 for _, _, y in pts], want, dirs)

    def test_oneway_bits_round_trip_through_parse_record(self):
        import collections
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN],
                                          hws=['residential'], country=352, dirs=[[1, -1]])
        w = struct.unpack('>%dH' % (len(plain) // 2), plain)
        _, _, shapes = fa_encode.parse_record(plain)
        by_link = collections.defaultdict(set)
        for k, (role, lid) in shapes[0]['points'].items():
            by_link[lid].add(w[k + 1] & 0xc000)
        self.assertEqual(by_link[1], {0x4000}, 'link 1 drawn with traffic')
        self.assertEqual(by_link[2], {0x8000}, 'link 2 drawn against traffic')

    def test_level2_style_word_clears_bit12_for_oneway(self):
        for dirs, want in (([1, -1], 0x001e), ([-1, 1], 0x001e), ([0, 0], 0x101e)):
            shape = fa_encode.shape_words(self.CHAIN, self.NODES, level=2, dirs=dirs)
            self.assertEqual(shape[9], want, dirs)   # length, npts, 5 header, 2 start point

    def test_a_two_point_shape_keeps_the_form_that_drew(self):
        shape = fa_encode.shape_words([(1, 0, 1, [])], self.NODES)
        self.assertEqual(shape[7:9], [0x0800 | 100, 0x2800 | 200])
        self.assertEqual(shape[12:14], [0x7000 | 900, 240])

    def test_names_section(self):
        labels = ['1=Hringvegur;2=R1', '1=Reykjanesbraut']
        items = [fa_encode.item_words([0, 0x8000 | 5], 1, 12, 352)]
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
        self.assertEqual(fa_encode.item_words(shape, 1, country=352)[2], 0x8000 | 2 * (7 - 2))
        level2 = fa_encode.item_words(shape, 2, name_off=12, country=352)
        self.assertEqual(level2[-3:], [1, 1, 12], 'k = 1 for a named level-2 shape')
        self.assertEqual(fa_encode.item_words(shape, 2, name_off=0xffff, country=352)[-3:], [1, 0, 0])
        for level in (1, 2):
            self.assertEqual(fa_encode.item_words(shape, level, country=578)[5], 578, 'word 5 is the country')

    def test_hws_must_match_the_chains(self):
        with self.assertRaises(IndexError):
            fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.chains()[:1],
                                   hws=['motorway', 'residential'], country=352)


class TestShapeTail(unittest.TestCase):
    """The level-1 tail table: a count word, then one run item per 127 of shapes + links, padded to a dword."""

    def test_counts(self):
        for n, want in ((0, []), (127, [0x7f01]), (128, [0x7f01, 0x0101]),
                        (255, [0x7f01, 0x7f01, 0x0101]), (256, [0x7f01, 0x7f01, 0x0201]),
                        (819, [0x7f01] * 6 + [0x3901])):
            got = fa_encode.tail_items(n)
            self.assertEqual(got, want, n)
            self.assertEqual(fa_encode.tail_count(got), n, n)
            self.assertLessEqual(max([x >> 8 for x in got] or [0]), 127, 'no item over 127')

    def test_factory_examples(self):
        self.assertEqual(fa_encode.tail_items(79 + 156), [0x7f01, 0x6c01])
        self.assertEqual(fa_encode.tail_items(42 + 85), [0x7f01])

    def test_round_trip_under_127(self):
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, TestRoadShapes.NODES, TestRoadShapes().chains(),
                                          hws=['motorway', 'residential'], country=352)
        self.assertEqual(fa_encode.tail_of(plain), (1, [0x0401]), 'shapes 2 + links 2')

    def test_round_trip_over_127(self):
        nodes = [(100, 200), (900, 240)]
        chains = [[(i, 0, 1, [])] for i in range(1, 131)]
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains,
                                          hws=['residential'] * 130, country=352)
        w = struct.unpack('>%dH' % (len(plain) // 2), plain)
        self.assertEqual(w[18], 6, 'tail grows to 6 dwords past 254')
        count, items = fa_encode.tail_of(plain)
        self.assertEqual(count, 3)
        self.assertEqual(items, fa_encode.tail_items(260))
        self.assertEqual(fa_encode.tail_count(items), 260)


class TestSpeedLimits(unittest.TestCase):
    """The level-1 style word carries the posted limit; factory Norway/England restyle a chain at a limit change instead of splitting it."""

    NODES = TestRoadShapes.NODES
    CHAIN = TestRoadShapes.CHAIN

    def test_style_word(self):
        self.assertEqual(fa_encode.style_word(None), 0x101e, 'no limit')
        self.assertEqual(fa_encode.style_word(50), 0x1029)
        self.assertEqual(fa_encode.style_word(30), 0x1025)
        for v in range(5, 165, 5):
            self.assertEqual(fa_encode.limit_of(fa_encode.style_word(v)), v)
        for v in (0, 52, 165):
            self.assertEqual(fa_encode.style_word(v), 0x101e, v)
        self.assertIsNone(fa_encode.limit_of(0x101e))

    def test_speed_limit(self):
        f = fa_encode.speed_limit
        self.assertEqual(f('50'), 50)
        self.assertEqual(f(' 80 '), 80)
        self.assertEqual(f('30 mph'), 50, '48 km/h, signed in steps of 5')
        self.assertEqual(f('30 mph', 'mph'), 30)
        self.assertEqual(f('50', 'mph'), 30)
        self.assertEqual(f('50;30'), 50)
        for v in (None, '', 'IS:urban', 'none', 'walk', 'signals', '52'):
            self.assertIsNone(f(v), v)

    def shape(self, limits):
        return fa_encode.shape_words(self.CHAIN, self.NODES, styles=[fa_encode.style_word(v) for v in limits])

    def test_one_limit_writes_one_word(self):
        plain = fa_encode.shape_words(self.CHAIN, self.NODES)
        same = self.shape([50, 50])
        self.assertEqual(len(same), len(plain), 'an unchanged limit is not repeated')
        self.assertEqual(same[9], 0x1029)
        self.assertEqual([i for i, v in enumerate(same) if v == 0x1029], [9])

    def test_a_change_restyles_the_next_link(self):
        sh = self.shape([50, 30])
        self.assertEqual(len(sh), len(self.shape([50, 50])) + 1)
        pts = len(self.CHAIN[0][3]) + 1
        end = 9 + 3 + 2 * pts                     # one past the first link's last point
        self.assertEqual(sh[end - 1] & 0xf800, 0x2800, 'the link end announces a style word')
        self.assertEqual(sh[end:end + 3], [0x1025, 0x0100, 2])

    def test_record_reads_back(self):
        CH = TestShapeIndex.CHAINS
        st = [[fa_encode.style_word(80), fa_encode.style_word(None)], [fa_encode.style_word(30)]]
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, TestShapeIndex.NODES, CH,
                                          hws=['trunk', 'residential'], styles=st, country=352)
        ix = fa_encode.build_record.last_index
        _, _, shapes = fa_encode.parse_record(plain)
        for S in ix['shapes']:
            got = next(s for s in shapes if s['at'] == S['at'])
            self.assertTrue(got['ok'])
            self.assertEqual(got['points'], {w: (role, lid) for w, lid, role in S['points']},
                             'the address index still names the right words')
            self.assertEqual(got['styles'], st[S['chain']])

    def test_rd_build_takes_maxspeed(self):
        pts = TestRoadChains.POINTS
        a = dict(id=1, hw='residential', name='Jonsgeisli', ref=None, maxspeed='30', nodes=pts[:3])
        b = dict(a, id=2, maxspeed='50', nodes=pts[2:])
        c = dict(id=3, hw='service', name=None, ref=None, nodes=[pts[1], (-21.896, 64.127)])
        parcels = rd_build.build_graphs([a, b, c], TestRoadChains.CELL, 1, True)
        was, rd_build.COUNTRY = rd_build.COUNTRY, 352
        try: recs = rd_build.encode_parcels(parcels, 1)
        finally: rd_build.COUNTRY = was
        (pc, rec), = recs.items()
        _, _, shapes = fa_encode.parse_record(rec[2])
        got = sorted(([fa_encode.limit_of(v) for v in s['styles']] for s in shapes), key=len)
        self.assertEqual(got, [[None], [30, 30, 50]], 'one shape restyled at the way change; no maxspeed, no limit')

    def test_rd_build_writes_oneway(self):
        # a runs east, b is drawn west but joins a as one chain, so chain_links flips it
        pts = TestRoadChains.POINTS
        a = dict(id=1, hw='residential', name='Jonsgeisli', ref=None, dir=1, nodes=pts[:3])
        b = dict(a, id=2, dir=1, nodes=pts[2:][::-1])
        c = dict(id=3, hw='service', name=None, ref=None, nodes=[pts[1], (-21.896, 64.127)])
        parcels = rd_build.build_graphs([a, b, c], TestRoadChains.CELL, 1, True)
        was, rd_build.COUNTRY = rd_build.COUNTRY, 352
        try: recs = rd_build.encode_parcels(parcels, 1)
        finally: rd_build.COUNTRY = was
        (pc, rec), = recs.items()
        self.assertEqual(rd_check.check(rec[0], 1, 352), ([], []))
        g = clarion_rd.parse_record(rec[0])
        ow, bad = rd_check.oneway_links(g)
        self.assertEqual(bad, [])
        split = rd_build.quant(pts[2], pc)[0]
        got = sorted(('a' if max(g['nodes'][i]['x'], g['nodes'][j]['x']) <= split else 'b',
                      g['nodes'][j]['x'] > g['nodes'][i]['x']) for i, j, _ in ow)
        self.assertEqual(got, [('a', True), ('a', True), ('b', False)], 'a eastbound, b westbound')
        _, _, shapes = fa_encode.parse_record(rec[2])
        styles = sorted(([v & 0x1000 for v in s['styles']] for s in shapes), key=len)
        self.assertEqual(styles, [[0x1000], [0, 0, 0]], 'bit 12 clear on one-way links only')

    def test_dir_falls_back_to_oneway(self):
        pts = TestRoadChains.POINTS
        a = dict(id=1, hw='residential', name='Jonsgeisli', ref=None, oneway=True, nodes=pts[:3])
        b = dict(a, id=2, oneway=True, nodes=pts[2:][::-1])
        c = dict(id=3, hw='service', name=None, ref=None, oneway=False, nodes=[pts[1], (-21.896, 64.127)])
        ways = [a, b, c]
        rd_build.derive_dir(ways)
        self.assertEqual([w['dir'] for w in ways], [1, 1, 0], 'oneway True/False gives dir 1/0')
        parcels = rd_build.build_graphs(ways, TestRoadChains.CELL, 1, True)
        was, rd_build.COUNTRY = rd_build.COUNTRY, 352
        try: recs = rd_build.encode_parcels(parcels, 1)
        finally: rd_build.COUNTRY = was
        (pc, rec), = recs.items()
        self.assertEqual(rd_check.check(rec[0], 1, 352), ([], []))
        g = clarion_rd.parse_record(rec[0])
        ow, bad = rd_check.oneway_links(g)
        self.assertEqual(bad, [])
        split = rd_build.quant(pts[2], pc)[0]
        got = sorted(('a' if max(g['nodes'][i]['x'], g['nodes'][j]['x']) <= split else 'b',
                      g['nodes'][j]['x'] > g['nodes'][i]['x']) for i, j, _ in ow)
        self.assertEqual(got, [('a', True), ('a', True), ('b', False)], 'a eastbound, b westbound')
        _, _, shapes = fa_encode.parse_record(rec[2])
        styles = sorted(([v & 0x1000 for v in s['styles']] for s in shapes), key=len)
        self.assertEqual(styles, [[0x1000], [0, 0, 0]], 'section 1 bit 12 clear on the one-way chain')

    def test_warns_on_bare_fast_roads(self):
        import io, contextlib
        ways = [dict(id=1, hw='trunk', name=None, ref=None, nodes=TestRoadChains.POINTS[:2]),
                dict(id=2, hw='residential', name=None, ref=None, nodes=TestRoadChains.POINTS[:2])]
        err = io.StringIO()
        with contextlib.redirect_stderr(err): rd_build.derive_dir(ways)
        self.assertIn('WARNING', err.getvalue())
        self.assertIn('1 of them', err.getvalue())
        self.assertEqual([w['dir'] for w in ways], [0, 0])

    def test_no_warning_when_fields_present(self):
        import io, contextlib
        ways = [dict(id=1, hw='trunk', name=None, ref=None, dir=1, nodes=TestRoadChains.POINTS[:2]),
                dict(id=2, hw='residential', name=None, ref=None, oneway=True, nodes=TestRoadChains.POINTS[:2])]
        err = io.StringIO()
        with contextlib.redirect_stderr(err): rd_build.derive_dir(ways)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual([w['dir'] for w in ways], [1, 1])


class TestLinkNumbering(unittest.TestCase):
    """Links are numbered 1..n per class in section-1 drawing order, with 0x2000 on the entry at the drawn last point (rd_check.link_pairing)."""

    def parcel(self, level=1):
        pts = TestRoadChains.POINTS
        ways = [dict(id=1, hw='residential', name='Jonsgeisli', ref=None, nodes=pts[:3]),
                dict(id=2, hw='trunk', name='Vesturlandsvegur', ref='1', nodes=[pts[2], (-21.893, 64.1315), pts[4]]),
                dict(id=3, hw='residential', name='Gvendargeisli', ref=None, nodes=[pts[1], (-21.897, 64.127)]),
                dict(id=4, hw='service', name=None, ref=None, nodes=[pts[3], (-21.892, 64.1333)]),
                dict(id=5, hw='primary', name='Hofdabakki', ref=None, nodes=[(-21.893, 64.1315), (-21.890, 64.1290)])]
        parcels = rd_build.build_graphs(ways, TestRoadChains.CELL, level, True)
        was, rd_build.COUNTRY = rd_build.COUNTRY, 352
        try: recs = rd_build.encode_parcels(parcels, level)
        finally: rd_build.COUNTRY = was
        (pc, rec), = recs.items()
        (pc, P), = parcels.items()
        return P, rec

    def test_links_run_in_layer_order(self):
        P, rec = self.parcel()
        layers = [fa_encode.layer_class(l[5], 1)[0] for l in P['links']]
        self.assertEqual(layers, sorted(layers))
        self.assertGreater(len(set(layers)), 1, 'the fixture spans several layers')

    def test_ids_restart_per_class(self):
        P, rec = self.parcel()
        ids = rd_build.link_ids(P['links'])
        per = {}
        for l, i in zip(P['links'], ids): per.setdefault(fa_encode.layer_class(l[5], 1)[1], []).append(i)
        self.assertEqual(sorted(per), [4, 5, 8, 9])
        for cls, lst in per.items(): self.assertEqual(lst, list(range(1, len(lst) + 1)), 'class %d' % cls)

    def test_sections_pair(self):
        P, rec = self.parcel()
        _, _, shapes = fa_encode.parse_record(rec[2])
        self.assertTrue(all(s['ok'] for s in shapes))
        n, bad = rd_check.link_pairing(shapes, clarion_rd.parse_record(rec[0]))
        self.assertEqual(bad, [])
        self.assertEqual(n, len(P['links']))
        self.assertEqual([s['links'] for s in shapes if s['cls'] == 4], [[1, 2]], 'the trunk draws as one shape, links 1 and 2')

    def test_level2_pairs_too(self):
        P, rec = self.parcel(2)
        _, _, shapes = fa_encode.parse_record(rec[2], 2)
        self.assertTrue(shapes and all(s['ok'] for s in shapes))
        n, bad = rd_check.link_pairing(shapes, clarion_rd.parse_record(rd_check.part_a(rec[0], 2)))
        self.assertEqual((n, bad), (len(P['links']), []))

    def test_pairing_rejects_the_old_numbering(self):
        P, rec = self.parcel()
        _, _, shapes = fa_encode.parse_record(rec[2])
        g = clarion_rd.parse_record(rec[0])
        for nd in g['nodes']:
            for e in nd['entries']: e['id'] ^= 0x2000      # flag at the drawn first point
        n, bad = rd_check.link_pairing(shapes, g)
        self.assertEqual(len(bad), len(P['links']))
        self.assertTrue(all('end flag' in b for b in bad))
        n, bad = rd_check.link_pairing(shapes, g, strict_flag=False)
        self.assertEqual((bad, rd_check.link_pairing.flag_off), ([], len(P['links'])))
        shapes[0]['links'][0] += 100
        n, bad = rd_check.link_pairing(shapes, clarion_rd.parse_record(rec[0]))
        self.assertTrue(any('want 1..' in b for b in bad), bad)

    def test_shape_index_names_links_by_class_and_id(self):
        P, rec = self.parcel()
        R = rd_build.encode_parcels.index[next(iter(rd_build.encode_parcels.index))]
        seen = [(S['cls'], l['id']) for S in R['shapes'] for l in S['links']]
        self.assertEqual(len(seen), len(set(seen)), '(class, id) is unique in the record')
        self.assertTrue(any(l['id'] == 1 for S in R['shapes'] for l in S['links'] if S['cls'] == 8))


class TestRoadChains(unittest.TestCase):
    """rd_build turns ways into links that carry their own bends, instead of a graph node at every bend."""

    CELL = 'B60R080R'
    POINTS = [(-21.900 + 0.002 * i, 64.130 + 0.0004 * i) for i in range(5)]   # one parcel
    WAY = dict(id=1, hw='residential', name='Jonsgeisli', ref=None, nodes=POINTS)

    def parcel(self, ways, chain=True):
        parcels = rd_build.build_graphs(ways, self.CELL, 1, chain)
        self.assertEqual(len(parcels), 1, 'the ways here sit in one parcel')
        return next(iter(parcels.values()))

    def test_a_bend_is_not_a_graph_node(self):
        P = self.parcel([self.WAY])
        self.assertEqual(len(P['links']), 1, 'one way run, one link')
        self.assertEqual(len(P['links'][0][8]), 3, 'the three bends ride inside the link')
        self.assertEqual(len(P['nodes']), 2, 'only the two ends are nodes')

    def test_no_chain_puts_a_node_at_every_point(self):
        P = self.parcel([self.WAY], chain=False)
        self.assertEqual(len(P['links']), 4)
        self.assertEqual([len(l[8]) for l in P['links']], [0, 0, 0, 0])
        self.assertEqual(len(P['nodes']), 5)

    def test_a_junction_splits_the_link_and_not_the_shape(self):
        side = dict(id=2, hw='service', name=None, ref=None,
                    nodes=[self.POINTS[2], (-21.896, 64.127)])
        P = self.parcel([self.WAY, side])
        mine = [l for l in P['links'] if l[3] == self.WAY['id']]
        self.assertEqual(len(mine), 2, 'the junction splits the run')
        self.assertEqual(len({l[9] for l in mine}), 1, 'both links keep one chain id')
        chains = [c for c in rd_build.chains_of(P['links'], P['index']) if len(c) == 2]
        self.assertEqual(len(chains), 1, 'one shape of two links')
        npts = 1 + sum(len(inner) + 1 for _, _, _, inner in chains[0])
        self.assertEqual(npts, 5, 'all five points of the way, in one shape')

    def test_a_chain_draws_with_the_link_flags_right(self):
        P = self.parcel([self.WAY])
        chain = rd_build.chains_of(P['links'], P['index'])[0]
        nodes = list(P['order'])
        shape = fa_encode.shape_words(chain, nodes)
        self.assertEqual(shape[1] & 0x3fff, 5)
        xs = [shape[i] for i in (7, 12, 14, 16, 18)]   # start, then the link's four points
        self.assertEqual([x & 0x0800 for x in xs[:-1]], [0x0800] * 4,
                         'the start and every point the link goes on past')
        self.assertEqual(xs[-1] & 0x0800, 0, 'the last point ends the link')


class TestShapeIndex(unittest.TestCase):
    """Address records point at x words inside a 0x00fa record; the index fa_encode keeps must name the words parse_record finds."""

    NODES = [(100, 200), (900, 240), (1500, 1800), (1600, 1900)]
    CHAINS = [[(1, 0, 1, [(300, 210), (600, 225)]), (2, 1, 2, [(1100, 900)])], [(3, 2, 3, [])]]

    def test_index_matches_parse(self):
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.CHAINS, hws=['trunk', 'residential'], country=352)
        ix = fa_encode.build_record.last_index
        code, unit, shapes = fa_encode.parse_record(plain)
        self.assertEqual((code, unit), (ix['code'], 22))
        self.assertEqual(len(shapes), 2)
        for S in ix['shapes']:
            got = next(s for s in shapes if s['at'] == S['at'])
            self.assertTrue(got['ok'])
            self.assertEqual(got['points'], {w: (role, lid) for w, lid, role in S['points']})

    def test_offsets_follow_kanren(self):
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, self.CHAINS[1:], hws=['residential'], country=352)
        S = fa_encode.build_record.last_index['shapes'][0]
        k, i = S['points'][0][0] - 22, S['points'][-1][0] - 22
        self.assertEqual(k, S['at'] + 7 - 22)
        self.assertEqual(i - k, 2 * 2 + 1)

    def test_roles(self):
        roles = [r for _, _, r in fa_encode.point_words(self.CHAINS[0])]
        self.assertEqual(roles, ['start', 'vertex', 'vertex', 'end', 'vertex', 'end'])


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
    """make_drstr builds DRSTR001.DAT; this reads it back per FORMAT.md, over a tree of empty files."""

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
        """MAPAL001 really holds 5,162,906,624 bytes and the field reads 867,939,328; the .ind header declares the size, no large file needed."""
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
    """Checks the local copies above against bg_encode (which needs shapely to import) for drift."""

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

    def test_landuse_layers(self):
        import json

        from shapely.geometry import box

        import bg_tiles
        lon0, lat0, w, h = clarion_bg.parcel_geo(1, 96, 898, 7, 9)
        wood = box(lon0 + w / 4, lat0 + h / 4, lon0 + w / 2, lat0 + h / 2)
        lake = wood.buffer(-w / 16)
        fc = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'kind': k}, 'geometry': g.__geo_interface__}
            for k, g in (('forest', wood), ('water', lake))]}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(fc, f)
        try:
            bg_tiles.load_landuse(f.name)
            extra = bg_tiles.landuse_layers(1, box(lon0, lat0, lon0 + w, lat0 + h), lon0, lat0, w, h)
        finally:
            bg_tiles.LANDUSE.update(items=[], tree=None)
            os.unlink(f.name)
        plain, _ = bg_encode.build_parcel_record(1, 96, 898, 7, 9, [], [], [], extra_layers=extra)
        r = clarion_bg.parse_record(plain)
        self.assertEqual(sorted(r['layers']), [3, 7])
        self.assertEqual([s['type'] for s in r['layers'][3]['shapes']], [0xc01a])
        self.assertEqual([s['type'] for s in r['layers'][7]['shapes']], [0xc015])
        self.assertEqual(clarion_bg.build_record(r), plain)

    def test_buildings_over_255_split_into_subgroups(self):
        import json

        from shapely.geometry import box

        import bg_tiles
        lon0, lat0, w, h = clarion_bg.parcel_geo(1, 96, 898, 7, 9)
        d = w / 40
        houses = [box(lon0 + i * d, lat0 + j * d, lon0 + (i + .5) * d, lat0 + (j + .5) * d)
                  for i in range(20) for j in range(20)]
        fc = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'kind': 'building'}, 'geometry': g.__geo_interface__} for g in houses]}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(fc, f)
        try:
            with self.assertRaises(SystemExit):
                bg_tiles.load_landuse(f.name)
            bg_tiles.load_landuse(f.name, {'building': (5, 0x020)})
            extra = bg_tiles.landuse_layers(1, box(lon0, lat0, lon0 + w, lat0 + h), lon0, lat0, w, h)
        finally:
            bg_tiles.LANDUSE.update(items=[], tree=None)
            os.unlink(f.name)
        plain, _ = bg_encode.build_parcel_record(1, 96, 898, 7, 9, [], [], [], extra_layers=extra)
        r = clarion_bg.parse_record(plain)
        g = r['layers'][5]
        self.assertEqual([len(s['shapes']) for s in [g] + g.get('more', [])], [255, 145])
        self.assertEqual({s['type'] for s in g['shapes']}, {0xc020})
        self.assertEqual(clarion_bg.build_record(r), plain)


class TestLandUseRules(unittest.TestCase):
    """extract_landuse.classify: Shortbread's land and water tags to AREA_CLASSES kinds."""

    def test_kinds(self):
        cases = [({'landuse': 'forest'}, 'forest'), ({'natural': 'wood'}, 'forest'),
                 ({'natural': 'water'}, 'water'), ({'natural': 'water', 'water': 'river'}, 'river'),
                 ({'waterway': 'riverbank'}, 'river'), ({'leisure': 'park'}, 'park'),
                 ({'natural': 'heath'}, 'heath'), ({'building': 'yes'}, None), ({'landuse': 'residential'}, None)]
        for tags, kind in cases:
            self.assertEqual(extract_landuse.classify(tags), kind, tags)
        self.assertEqual(extract_landuse.classify({'building': 'house', 'landuse': 'forest'}, True), 'building')
        self.assertEqual(extract_landuse.classify({'building': 'no', 'landuse': 'forest'}, True), 'forest')

    def test_every_kind_has_a_class(self):
        for _, _, kind in extract_landuse.RULES:
            self.assertIn(kind, clarion_bg.AREA_CLASSES)


if __name__ == '__main__':
    unittest.main()


class AddreTrie(unittest.TestCase):
    """The ADDRE letter trie and the ZCODE digit trie, built from code."""

    def keys(self, kw, words):
        out = []
        for i, w in enumerate(sorted(words)):
            k = b''.join((c.encode('utf-8') + b'\0' * kw)[:kw] for c in w)
            out.append((k, i, 1))
        return out

    def walk(self, blob, cr, kw, recw):
        """Read the keys back out of a built node region."""
        def crec(o):
            off = int.from_bytes(blob[o:o + 4], 'big')
            size = int.from_bytes(blob[o + 4:o + 6], 'big')
            return off, size, blob[o + 8:o + 8 + kw], \
                int.from_bytes(blob[o + 8 + kw:o + cr], 'big')
        o, root = 0, []
        while int.from_bytes(blob[o:o + 4], 'big'):
            root.append(crec(o)); o += cr
        out, stack = [], [(c, b'') for c in root]
        while stack:
            c, pfx = stack.pop()
            k = pfx + c[2]
            if c[1] == 0:
                out.append((k, c[0] // recw, c[3])); continue
            fo = c[0] * 2
            n = c[1] * 2 // cr - 1
            own = blob[fo + n * cr + 8 + cr - 9]
            if own:
                out.append((k, int.from_bytes(blob[fo + n * cr:fo + n * cr + 4], 'big') // recw, own))
            for i in range(n):
                stack.append((crec(fo + i * cr), k))
        return sorted(out)

    def test_latin_one_byte_keys(self):
        from addre_io import build_trie
        keys = self.keys(1, ['AZEGLIO', 'BATTISTI', 'BELLO', 'PIAZZA SAN PIETRO', 'PIAZZALE'])
        blob, first, nroot = build_trie(keys, 12, 1)
        self.assertEqual(len(blob) % 4, 0)
        self.assertEqual(self.walk(blob, 12, 1, 11), keys)

    def test_two_byte_keys_hold_non_ascii(self):
        from addre_io import build_trie
        keys = self.keys(2, ['ÞORLÁKSGEISLI', 'ÞÓRSGATA', 'HÖFÐABAKKI', 'AUSTURSTRÆTI'])
        blob, first, nroot = build_trie(keys, 14, 2)
        self.assertEqual(self.walk(blob, 14, 2, 11), keys)
        self.assertEqual(nroot, 3)                      # A, H and Thorn

    def test_prefix_key_gets_an_own_count(self):
        from addre_io import build_trie
        keys = self.keys(1, ['BERG', 'BERGSTADIR'])
        blob, _, _ = build_trie(keys, 12, 1)
        self.assertEqual(self.walk(blob, 12, 1, 11), keys)

    def test_name_block_codec(self):
        """The 58 85 block is zlib with a 4 KiB window and memLevel 6."""
        from addre_io import deflate_block, inflate_block
        plain = b'PIAZZA SAN PIETRO\x00' * 40
        c = deflate_block(plain)
        self.assertEqual(c[:2], b'\x58\x85')
        self.assertEqual(inflate_block(c, 0, (len(c) + 1) // 2), plain)

    def test_zcode_digits(self):
        from zcode_io import assemble
        codes = ['101', '113', '200', '800']
        levels = {'101': (12, 20, 0), '113': (12, 20, 0),
                  '200': (12, 20, 1), '800': (12, 22, 0)}
        prefix = bytearray(0x1100)
        prefix[0x1032], prefix[0x1033] = 6, 1
        out = assemble(bytes(prefix), codes, levels)
        self.assertEqual(len(out) % 0x100, 0)
        self.assertEqual(int.from_bytes(out[0x1016:0x101a], 'big'), 4 * 8)
        self.assertEqual(int.from_bytes(out[0x101a:0x101c], 'big'), 8)

    def test_zcode_writes_its_own_record_block_address(self):
        """0x1012 is the record block's sector address (exact on 36 factory files); a donor value can point past a smaller file's end, which crashed the postcode screen."""
        from addre_io import addr, unaddr
        from zcode_io import assemble
        codes = ['101', '113', '200', '800']
        levels = {c: (12, 20, 0) for c in codes}
        prefix = bytearray(0x1100)
        prefix[0x1032], prefix[0x1033] = 6, 1
        struct.pack_into('>I', prefix, 0x1012, 10498)       # Italy's own value
        out = assemble(bytes(prefix), codes, levels)
        nodew = int.from_bytes(out[0x1006:0x100a], 'big')
        srb = (0x1100 + nodew * 2 + 0xff) // 0x100 * 0x100
        self.assertEqual(struct.unpack_from('>I', out, 0x1012)[0], addr(srb))
        self.assertLess(unaddr(struct.unpack_from('>I', out, 0x1012)[0]), len(out))

    def test_zcode_record_carries_each_code_own_area(self):
        from zcode_io import ZREC, assemble
        codes = ['101', '800']
        levels = {'101': (12, 20, 0), '800': (12, 22, 0)}
        prefix = bytearray(0x1100)
        prefix[0x1032], prefix[0x1033] = 6, 1
        out = assemble(bytes(prefix), codes, levels)
        nodew = int.from_bytes(out[0x1006:0x100a], 'big')
        srb = (0x1100 + nodew * 2 + 0xff) // 0x100 * 0x100
        for i, c in enumerate(codes):
            r = out[srb + i * ZREC:srb + (i + 1) * ZREC]
            self.assertEqual(struct.unpack_from('>3H', r, 0), levels[c])
            self.assertEqual(r[6:14], b'\xff' * 8)
            self.assertEqual(r[14:], b'\x00\x00')

    def test_assemble_fills_its_own_header_fields(self):
        from addre_io import Addre, addr, assemble
        keys = self.keys(1, ['AZEGLIO', 'BATTISTI', 'BELLO'])
        srecs = [b'\x7f\x7f\x00\x00' + b'\xff' * 18 for _ in range(5)]
        names = [b'AZEGLIO\x00', b'BATTISTI', b'BELLOX\x00']
        prefix = bytearray(0x1100)
        srec_pre = struct.pack('>H', 7) + b'\x00' * 8
        df = bytearray(0x100)
        df[0x49] = 0xaa                       # a sentinel: must survive untouched
        out, blocks = assemble(prefix, keys, srecs, names, bytes(df), srec_pre,
                                12, 1, area=7, ntier=1)

        a = Addre(out)
        srb = a.node_end + (-a.node_end) % 0x100
        cmpw = sum(cw for _, cw, _ in blocks)
        self.assertEqual(struct.unpack_from('>I', out, 0x1012)[0], addr(srb))
        self.assertEqual(struct.unpack_from('>I', a.df, 0x24)[0], addr(a.nb))           # A
        self.assertEqual(struct.unpack_from('>I', a.df, 0x28)[0], cmpw)                 # B
        self.assertEqual(struct.unpack_from('>H', a.df, 0x30)[0],
                          max(len(n) for n in names) // 2)                              # C
        self.assertEqual(struct.unpack_from('>I', a.df, 0x7a)[0], addr(a.tail_off))     # P
        self.assertEqual(struct.unpack_from('>I', a.df, 0x7e)[0], (4 + 12 * len(blocks)) // 2)
        self.assertEqual(struct.unpack_from('>I', out, 0x824 + 6)[0], addr(a.nb - 0x100))
        self.assertEqual(a.df[0x49], 2 + 1)   # 2 + ntier, overwritten despite the sentinel
        self.assertEqual(a.ntier, 1)

    def test_street_records_found_without_a_7f7f_relation(self):
        """Denmark and Norway lead with a street-ID record instead of 7f7f, so a 7f7f search lands two records late; 0x1012 gives the block outright."""
        from addre_io import Addre, assemble
        keys = self.keys(1, ['BERG', 'DALUR'])
        srecs = [b'\x00\x00\x40\x00' + b'\xff' * 18,          # no 7f7f on the first two
                 b'\x00\x00\x40\x00' + b'\xff' * 18,
                 b'\x7f\x7f\x00\x00' + b'\xff' * 18]
        names = [b'BERG\x00\x00', b'DALUR\x00']
        prefix = bytearray(0x1100)
        srec_pre = struct.pack('>H', 7) + b'\x00' * 8
        out, _ = assemble(prefix, keys, srecs, names, bytes(bytearray(0x100)),
                           srec_pre, 12, 1, area=7, ntier=1)
        a = Addre(out)
        self.assertEqual(a.srec(0), srecs[0])
        self.assertEqual(a.srec(2), srecs[2])
        self.assertEqual(a.srec_pre, srec_pre)

    def test_fold_key_strips_the_diacritic_and_keeps_the_letter(self):
        from addre_io import fold_key
        for name, key in [('FRANÇOIS', 'FRANCOIS'),
                          ('CAMÍ DE CANÒLICH', 'CAMI DE CANOLICH'),
                          ('TVÆRVEJ', 'TVARVEJ'),          # AE -> A
                          ('AGERSØ', 'AGERSO'),
                          ('AARSÆTHERVEGEN', 'AARSATHERVEGEN'),
                          ('MÜNCHNER', 'MUNCHNER'),
                          ('KAČIĆ', 'KACIC'),
                          ('ĐAKOVO', 'DAKOVO'),            # D-stroke -> D
                          ('DŰLŐ', 'DULO'),
                          ('ŁĄKOWA', 'LAKOWA'),
                          ('MUÑOZ', 'MUNOZ'),
                          ('AĞAÇ', 'AGAC'),
                          ('AGGER-THYBORØN', 'AGGER THYBORON'),
                          ('AA. LOUIS', 'AA LOUIS')]:
            self.assertEqual(fold_key(name), key, name)

    def test_fold_key_takes_extra_mappings(self):
        """Þ and Ð are in no factory name, so the convention for them is the caller's."""
        from addre_io import fold_key
        extra = {'Þ': 'TH', 'Ð': 'D'}
        self.assertEqual(fold_key('ÞORLÁKSGEISLI', extra), 'THORLAKSGEISLI')
        self.assertEqual(fold_key('SAFNATRÖÐ', extra), 'SAFNATROD')
        self.assertEqual(fold_key('SÆVARHÖFÐI', extra), 'SAVARHOFDI')
        self.assertTrue(fold_key('ÞORLÁKSGEISLI', extra).isascii())

    def test_assemble_leaves_unresolved_fields_unless_given(self):
        from addre_io import assemble
        keys = self.keys(1, ['BERG'])
        srecs = [b'\x7f\x7f\x00\x00' + b'\xff' * 18]
        names = [b'BERG\x00\x00']
        prefix = bytearray(0x1100)
        srec_pre = struct.pack('>H', 7) + b'\x00' * 8
        df = bytearray(0x100)
        df[0x32:0x34] = b'\x12\x34'            # D: a donor value that must pass through
        df[0x63] = 0x56                        # name-width hint: same
        out, _ = assemble(prefix, keys, srecs, names, bytes(df), srec_pre, 12, 1,
                           area=7, ntier=0)
        from addre_io import Addre
        a = Addre(out)
        self.assertEqual(a.df[0x32:0x34], b'\x12\x34')
        self.assertEqual(a.df[0x63], 0x56)

        out2, _ = assemble(prefix, keys, srecs, names, bytes(df), srec_pre, 12, 1,
                            area=7, ntier=0, maxkey_hint=99, namewidth_hint=50)
        a2 = Addre(out2)
        self.assertEqual(struct.unpack_from('>H', a2.df, 0x32)[0], 99)
        self.assertEqual(a2.df[0x63], 50)


class AddreRecords(unittest.TestCase):
    """The ADDRE name and street records and the area ladder they carry; expected bytes are Andorra's ADDRE002, whose three-level ladder (COUNTRY/PARROQUIA/SETTLEMENT) matches Iceland's shape."""

    ANDORRA_NAME_0 = bytes.fromhex(
        'c02106 0c 9feccecd 02402 13b 03402143'.replace(' ', '')
        + '065400003a31333a3b3a353a'
        + '065800003a31303a3b3a323a'
        + '0309000100030000'
        + '4156494e475544412043 41524c454d414e59'.replace(' ', ''))
    ANDORRA_SREC_0 = bytes.fromhex('7f7f4000ffffffff000001c8000100020000ffffffff')

    def test_name_record_matches_andorra(self):
        from addre_io import SIDE_EVEN, SIDE_ODD, house_item, name_record
        items = house_item(13, 5, SIDE_ODD) + house_item(10, 2, SIDE_EVEN)
        r = name_record('AVINGUDA CARLEMANY', 0x9fec, 0xcecd, (1, 3, 0), items,
                        refs=((2, 0x40, 0x213b), (3, 0x40, 0x2143)))
        self.assertEqual(r, self.ANDORRA_NAME_0)

    def test_continuation_record_has_no_ladder(self):
        from addre_io import SIDE_EVEN, SIDE_ODD, house_item, name_record
        items = house_item(19, 15, SIDE_ODD) + house_item(16, 12, SIDE_EVEN)
        r = name_record('', 0x9fec, 0xcecd, (), items,
                        refs=((2, 0x00, 0x2137), (3, 0x40, 0x213b)), flags=0x19)
        self.assertEqual(r, bytes.fromhex(
            'c817060e9feccecd020021370340213b'
            '075400003a31393a3b3a31353a00075800003a31363a3b3a31323a000000'))

    def test_street_record_matches_andorra(self):
        from addre_io import street_record
        self.assertEqual(street_record(0x4000, 456, (1, 2, 0)), self.ANDORRA_SREC_0)

    def test_street_record_takes_the_factory_id_form(self):
        from addre_io import street_record
        self.assertEqual(street_record(0x4000, 646, (1, 2, 1), rel=0, sid=0x00010005),
                         bytes.fromhex('00004000000100050000028600010002 0001ffffffff'
                                       .replace(' ', '')))

    def test_five_levels_fill_the_record(self):
        """Italy declares five tier labels and its street record has no padding left."""
        from addre_io import SREC, street_record
        r = street_record(0x4000, 0x236747, (12, 7, 3, 0x23, 2))
        self.assertEqual(len(r), SREC)
        self.assertEqual(r[-2:], b'\x00\x02')

    def test_house_item_pads_to_a_word(self):
        from addre_io import SIDE_ODD, house_item
        self.assertEqual(house_item(13, 5, SIDE_ODD),
                         bytes([6, SIDE_ODD, 0, 0]) + b':13:;:5:')
        self.assertEqual(house_item(19, 15, SIDE_ODD),
                         bytes([7, SIDE_ODD, 0, 0]) + b':19:;:15:\x00')


class AddreParcelWords(unittest.TestCase):
    """A name record's lat and lon words are a level-1 parcel code (REPORT-kanren)."""

    def test_known_codes(self):
        from addre_io import parcel_words
        self.assertEqual(parcel_words(64.1466, -21.9426), (0xb01f, 0xc30d), 'Reykjavik')
        self.assertEqual(parcel_words(41.9022, 12.4533), (0x9f6f, 0xd43e), 'the Vatican record')

    def test_inverse(self):
        import addr_resolve
        from addre_io import parcel_point
        la, lo, y, x = parcel_point(64.1245, -21.7456)
        row, col = addr_resolve.code_rc(la, lo)
        self.assertEqual(clarion_bg.parcel_code(1, row, col), (la, lo))
        self.assertTrue(0 <= y < 2048 and 0 <= x < 2048)

    def test_refs_and_parse(self):
        from addre_io import house_item, name_record, parse_name_record, street_refs
        self.assertEqual(street_refs(80, 107), ((2, 0x40, 80), (3, 0x40, 107)))
        self.assertEqual(street_refs(107, 80, i_node=False), ((4, 0x40, 107), (5, 0, 80)))
        r = name_record('ÞORLÁKSGEISLI', 0xb01d, 0xc32c, (0, 0, 0), house_item(1, 51), refs=street_refs(80, 107))
        got = parse_name_record(r)
        self.assertEqual((got['name'], got['lat'], got['lon'], got['levels']), ('ÞORLÁKSGEISLI', 0xb01d, 0xc32c, (0, 0, 0)))
        self.assertEqual(got['refs'], [(2, 0x40, 80), (3, 0x40, 107)])


class IcodeTree(unittest.TestCase):
    """The ICODE area-name chain: rows, terminators and the size field."""

    def rows_of(self, blob, off=0):
        import icode_io
        return icode_io.read_list(blob, off)

    def test_chain_round_trips(self):
        import icode_io
        rows = [icode_io.pack_row('REYKJAVIK', 100, 44, 0),
                icode_io.pack_row('KOPAVOGUR', 150, 22, 1)]
        blob = icode_io.pack_list(rows, 7)
        back = self.rows_of(blob)
        self.assertEqual([r.name for r in back], [b'REYKJAVIK', b'KOPAVOGUR'])
        self.assertEqual([r.roff for r in back], [100, 150])
        self.assertEqual([r.idx for r in back], [0, 1])
        self.assertEqual(blob[-4:], (7).to_bytes(4, 'big'))

    def test_leaf_rows_carry_the_no_sub_bit(self):
        import icode_io
        blob = icode_io.pack_list([icode_io.pack_row('AKRANES', 0, 0, 0, leaf=True)], 3)
        r = self.rows_of(blob)[0]
        self.assertTrue(r.nosub)
        self.assertEqual(r.roff, 0xffffffff)
        self.assertEqual(r.cnt, 0)

    def test_deepest_chain_takes_two_terminators(self):
        import icode_io
        rows = [icode_io.pack_row('AKRANES', 0, 0, 0, leaf=True)]
        short = icode_io.pack_list(rows, 3)
        deep = icode_io.pack_list(rows, 3, deepest=True)
        self.assertEqual(len(deep) - len(short), icode_io.TERM * 2)
        self.assertEqual(deep[len(short) - 12 + 8:len(short) - 12 + 12],
                         (1).to_bytes(4, 'big'))        # the row count

    def test_name_field_width_is_kept_on_a_rename(self):
        import icode_io
        r = icode_io.pack_row('REYKJAVIK', 5, 6, 13, width=6)
        self.assertEqual(len(r), 12 + 12)
        self.assertEqual(self.rows_of(r + b'\x00\x06\x00\x00' + b'\xff' * 8)[0].name,
                         b'REYKJAVIK')

    def test_iso_slots_zero_the_unused_ladder(self):
        import icode_io
        b = icode_io.iso_slots(['COUNTRY', 'REGION', 'SETTLEMENT'])
        self.assertEqual(len(b), icode_io.NSLOT * icode_io.SLOT)
        self.assertEqual(b[:9], b'2=COUNTRY')
        self.assertEqual(b[3 * icode_io.SLOT:], b'\x00' * (4 * icode_io.SLOT))


class TestReferHeader(unittest.TestCase):
    def test_header_round_trips(self):
        import refer_header
        slots = {6: (0x00000010, 1), 7: (0, 2), 278: (0x40, 3)}
        h = refer_header.header(slots, (4160, 3520, -1680, -720), '14/11/17/01')
        self.assertEqual(len(h), 0x1000)
        self.assertEqual(refer_header.slots_of(h), slots)
        self.assertEqual(h[0x0e0:0x0e0 + 48].decode('utf-16-be'), 'DATA VERSION 14/11/17/01')
        self.assertTrue(refer_header.check(h))

    def test_adm_frame_is_aligned(self):
        import frames
        names = [('REYKJAVÍK', (0, 0), 20), ('KÓPAVOGUR', (0, 1), 20)]
        keys = {'RE'.encode('utf-16-be'): [(0, False)], 'KO'.encode('utf-16-be'): [(1, False)]}
        out, at, dfo = frames.adm(0x2000, 0, 2, names, keys)
        self.assertEqual(at, 0x2000)
        self.assertEqual(dfo % 256, 0)
        self.assertEqual(len(out) % 256, 0)


class TestHouse(unittest.TestCase):
    def test_names_and_parcel_codes(self):
        import house_encode as h
        self.assertEqual(h.file_name(0x3f, 4, 852, 7), 'B3F40AC7.DAT')
        self.assertEqual(h.cell_of(-21.77, 64.12), (96, 1, 902, 1))
        # the stock US file B3F40AC7 lists these codes for rows/columns (0,0), (0,4), (15,15)
        self.assertEqual(h.code(0x3f, 4, 0, 852, 7, 0), (0x9fc0, 0xaa70))
        self.assertEqual(h.code(0x3f, 4, 0, 852, 7, 4), (0x9fc0, 0xaa71))
        self.assertEqual(h.code(0x3f, 4, 15, 852, 7, 15), (0x9fcf, 0xaa7f))

    def test_record_layout(self):
        import house_encode as h
        layer, cls = h.KIND_CLASSES['building']
        sq = dict(type=cls, pts=[(10, 10), (10, 40), (40, 40), (40, 10), (10, 10)])
        r = h.record(0xb010, 0xc310, {layer: [(h.AREA, 2, [sq] * 300)]})
        w = struct.unpack_from('>24H', r)
        self.assertEqual(w[2], 0x000f)
        self.assertEqual(8 * (w[7] + w[8]), len(r))
        base = 8 * w[7]
        off, ln = struct.unpack_from('>HH', r, base + 4 * layer)
        g = base + 8 * off
        self.assertEqual(struct.unpack_from('>HH', r, g), (2, 5))       # 300 shapes, two sub-groups
        self.assertEqual((r[g + 4], r[g + 5], r[g + 8], r[g + 9]), (0x80, 255, 0x80, 45))
        self.assertEqual(struct.unpack_from('>5H', r, g + 10), (5, cls, 2, 0, 0xffff))
