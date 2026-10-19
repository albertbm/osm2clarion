#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write LEV3456R.DAT from a coastline and a profile alone: sea and coast at levels 6 to 3.

No existing map file is read. The records come from bg_tiles.encode_level_fast, the
container from lev_file.write and the header from lev_file.header. Section 2 (coarse
roads) is declared with its four level rows at count 0 and no directories, the form the
tiles need.

    level 6   one record for the whole database box
    level 5   one 11 x 11 directory over the database box, parcels in the region's cells
    level 4   one 44 x 44 directory the same way, the record in the odd pair of its column
    level 3   one 16 x 16 directory per 8-mesh square of the cells

Parcels outside the profile's cells are left absent. The sea label goes on level 3 only.

    lev_build.py --profile iceland [--coastline PATH] [--roads roads.json] --data-version yy/mm/dd/nn OUT.DAT

--roads adds road shapes at levels 3, 4 and 5 (lev_roads.build): the road-shape record in the
even pair of a level-3 or level-4 column, a road block inside a level-5 record, and route
numbers in the background records' names sections. Section 2 stays empty: the coarse graph
is routing data and is not written.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shapely.geometry import box  # noqa: E402

import bg_encode  # noqa: E402
import lev_roads  # noqa: E402
import bg_tiles  # noqa: E402
import clarion_bg  # noqa: E402
import clarion_profile  # noqa: E402
import lev_file  # noqa: E402
import tile_header  # noqa: E402
from clarion_bg import LEVELS, mesh_to_lat, mesh_to_lon, tile_cell  # noqa: E402

# per level: directory type, pairs per column, head x stored as x0 - 1024
DIRS = {6: (0x80, 1, False), 5: (0x90, 1, True), 4: (0x91, 2, True), 3: (0x91, 2, False)}
L6_TYPE = 0x0fa0            # level 6's record kind in every stock file
SEA6, COAST6 = 0x8012, 0x8007


def cells_box(cells):
    """(y0, y1, x0, x1) in meshes over a list of tile cells."""
    ys, xs = zip(*(tile_cell(c) for c in cells))
    return min(ys), max(ys) + 8, min(xs), max(xs) + 8


def mesh_box(y0, y1, x0, x1):
    return box(mesh_to_lon(x0), mesh_to_lat(y0), mesh_to_lon(x1), mesh_to_lat(y1))


def arcmin(y0, y1, x0, x1):
    """A mesh box as (lat max, lat min, lon min, lon max) arc-minutes."""
    return (round(mesh_to_lat(y1) * 60), round(mesh_to_lat(y0) * 60),
            round(mesh_to_lon(x0) * 60), round(mesh_to_lon(x1) * 60))


def no_holes(poly):
    """Cut a polygon at its holes until none is left: a record's polygons have no holes."""
    if not poly.interiors:
        return [poly]
    x = poly.interiors[0].centroid.x
    x0, y0, x1, y1 = poly.bounds
    out = []
    for half in (box(x0 - 1, y0 - 1, x, y1 + 1), box(x, y0 - 1, x1 + 1, y1 + 1)):
        g = poly.intersection(half)
        for p in (g.geoms if hasattr(g, 'geoms') else [g]):
            if p.geom_type == 'Polygon' and p.area > 0:
                out += no_holes(p)
    return out


def level6(dbm, land):
    """The whole-database record: sea everywhere but the land, and the coast."""
    y0, y1, x0, x1 = dbm
    lon0, lat0 = mesh_to_lon(x0), mesh_to_lat(y0)
    w, h = float(x1 - x0), (y1 - y0) / 1.5
    land6, coast6, _ = bg_tiles.clean_land(land, 6)
    sea = mesh_box(*dbm).difference(land6)
    polys = [s for g in (sea.geoms if hasattr(sea, 'geoms') else [sea]) if g.geom_type == 'Polygon'
             for p in no_holes(g) for s in [bg_encode.ring_shape(p.exterior, lon0, lat0, w, h, SEA6)] if s]
    coast = [s for ln in coast6 for s in bg_encode.line_shapes(ln, lon0, lat0, w, h, COAST6)]
    assert len(polys) <= 255 and len(coast) <= 255, (len(polys), len(coast))
    hdr = [0] * 22
    hdr[0], hdr[1] = clarion_bg.parcel_code(6, 2 * y0, 2 * x0)
    hdr[2], hdr[3], hdr[4] = L6_TYPE, 0xffff, 0xffff
    hdr[7], hdr[13], hdr[15], hdr[17], hdr[20] = 0xffff, 0xffff, 0xffff, 0xffff, 0xffff
    layers = {1: dict(kind=0x80, shapes=polys)}
    if coast:
        layers[11] = dict(kind=0x40, shapes=coast)
    rec = clarion_bg.build_record(dict(hdr=hdr, nlayers=26, layers=layers, sections={}))
    return rec + bytes(len(rec) % 2), len(polys), len(coast)


def strip_names(plain):
    r = clarion_bg.parse_record(plain)
    r['sections'].pop('names', None)
    return clarion_bg.build_record(r)


def directory(level, y0, x0, recs, labels):
    """recs {row: {col: (plain, comp)}} as a lev_file directory."""
    L = LEVELS.get(level, dict(rows=1, cols=1, dir_meshes=None))
    typ, ppc, xoff = DIRS[level]
    rows, cols = L['rows'], L['cols']
    D = dict(box=None, cols=cols, y=y0, x=(x0 - 1024) & 0xffff if xoff else x0, typ=typ,
             pairs=cols * ppc, rows=[None] * rows)
    for row, byc in recs.items():
        pairs = [None] * (cols * ppc)
        for col, (plain, _) in byc.items():
            pairs[col * ppc + ppc - 1] = lev_file.zrec(plain if labels else strip_names(plain))
        D['rows'][row] = pairs
    return D


def build(P, coast_path, data_version, ways=None):
    bg_tiles.use_profile(P)
    land, _, _ = bg_tiles.load_land(coast_path)
    dby0, dbx0 = P.mesh_origin
    dbm = (dby0, dby0 + 88, dbx0, dbx0 + 88)
    cy0, cy1, cx0, cx1 = cells_box(P.cells)
    cover = mesh_box(cy0, cy1, cx0, cx1)
    dbbox = (dbm[1], dbm[0], dbm[2], dbm[3])      # (y1, y0, x0, x1), the table's order
    levels = {}

    plain6, npoly, ncoast = level6(dbm, land)
    D6 = dict(box=dbbox, cols=1, y=dby0, x=dbx0, typ=0x80, pairs=1, rows=[[lev_file.rawrec(plain6)]])
    levels[6] = [D6]
    print('level 6: %d sea polygons, %d coast lines, %d B' % (npoly, ncoast, len(plain6)))

    for level in (5, 4):
        land_l, coast_l, tree_l = bg_tiles.clean_land(land, level)
        recs = bg_tiles.encode_level_fast(level, dby0, dbx0, land_l, coast_l, tree_l, cover)
        D = directory(level, dby0, dbx0, recs, labels=False)
        D['box'] = dbbox
        levels[level] = [D]
        print('level %d: %d parcels' % (level, sum(len(c) for c in recs.values())))

    land3, coast3, tree3 = bg_tiles.clean_land(land, 3)
    levels[3] = []
    for y0 in range(cy0, cy1, 8):
        for x0 in range(cx0, cx1, 8):
            recs = bg_tiles.encode_level_fast(3, y0, x0, land3, coast3, tree3, cover)
            if recs:
                D = directory(3, y0, x0, recs, labels=True)
                D['box'] = (y0 + 8, y0, x0, x0 + 8)
                levels[3].append(D)
    print('level 3: %d directories, %d parcels' % (
        len(levels[3]), sum(r is not None for D in levels[3] for row in D['rows'] if row for r in row)))

    if ways:
        for level, (nrec, nsh, npt, nlab) in sorted(lev_roads.build(levels, ways, (dby0, dbx0), P.country).items()):
            print('level %d roads: %d records, %d shapes, %d points, %d route labels' % (level, nrec, nsh, npt, nlab))

    cells = arcmin(cy0, cy1, cx0, cx1)
    sec1 = dict(id=1, levels=[dict(level=l, typ=t, dirs=levels.get(l, [])) for l, t in tile_header.LEVELS[1]])
    sec2 = dict(id=2, levels=[dict(level=l, typ=t, dirs=[]) for l, t in tile_header.LEVELS[2]])
    # box rows: the database box at levels 6 and 5, as stock files have; the data at 4 and 3
    boxes = [(6, 0, tuple(P.bounds)), (5, 0, tuple(P.bounds)), (4, 0, cells), (3, 0, cells)]
    fields = dict(box_rows=[(1, boxes), (2, [])], db_bounds=tuple(P.bounds),
                  data_version=data_version, source=None)
    return lev_file.write(dict(fields=fields, sections=[sec1, sec2]))


def main(argv):
    P = clarion_profile.take_arg(argv)
    coast = P.coastline
    if '--coastline' in argv:
        i = argv.index('--coastline')
        coast = argv[i + 1]
        del argv[i:i + 2]
    ways = None
    if '--roads' in argv:
        import json
        i = argv.index('--roads')
        ways = json.load(open(argv[i + 1]))
        del argv[i:i + 2]
    i = argv.index('--data-version')
    version = argv[i + 1]
    del argv[i:i + 2]
    out = build(P, coast, version, ways)
    with open(argv[0], 'wb') as f:
        f.write(out)
    print('wrote %s %d B' % (argv[0], len(out)))


if __name__ == '__main__':
    main(sys.argv[1:])
