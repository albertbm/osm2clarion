#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background tiles for a region's cells, from an OSM coastline. Sea and land only.

One .DAT per cell, levels 2 and 1. The header comes from a template tile taken off the
base card or, with --data-version, is written from nothing by tile_header.py, so no
existing tile is read. Roads go in afterwards with rd_build.py. The coarse levels 3 to 6 live in LEV3456R.DAT
and are handled by the lev_* tools, which import this module for load_land, clean_land
and encode_level_fast.

    ./bg_tiles.py --profile iceland TEMPLATE.DAT OUTDIR [CELL...]
    ./bg_tiles.py --profile iceland --data-version 14/11/17/01 [--no-road-section] OUTDIR [CELL...]

--no-road-section writes section 1 alone, with no empty section 2 declared.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shapely.geometry import box, LineString, MultiLineString
from shapely.ops import unary_union, linemerge, polygonize
from shapely.strtree import STRtree
from shapely.prepared import prep
import bg_encode, clarion_bg, clarion_profile
from clarion_bg import LEVELS, tile_cell, mesh_to_lon, mesh_to_lat, parcel_geo

BOUNDS = clarion_profile.EUROPE_BOUNDS
LAND = dict(clarion_profile.LAND)
SEA = ''
# Extra features drawn into every level. areas: (polygon, label); lines: LineString;
# points: (lon, lat, text). Empty for a coastline-only build.
MARKERS = dict(areas=[], lines=[], points=[])

t0 = time.time()


def use_profile(P):
    """Install a profile's region settings. Every entry point here reads these."""
    global BOUNDS, LAND, SEA
    BOUNDS, LAND, SEA = P.bounds, P.land, P.sea


def load_land(path):
    """Coastline ways -> (land multipolygon, ways, STRtree of ways)."""
    coast = bg_encode.load_geojson_lines(path)
    merged = linemerge(MultiLineString(coast))
    parts = merged.geoms if hasattr(merged, 'geoms') else [merged]
    rings = [g for g in parts if g.is_ring]
    land = unary_union(list(polygonize(rings)))
    print(f'coast ways {len(coast)}, closed rings {len(rings)}, '
          f'open pieces {len(parts) - len(rings)}, land area {land.area:.3f} deg2')
    return land, coast, STRtree(coast)


def clean_land(land, level):
    """Land, coast lines and their index for one level, drawn from the outline itself.

    Islands below the level's area floor are dropped and the rest simplified, because the
    shipped map shows skerries only when zoomed in close."""
    min_area, tol = LAND[level]
    polys = [p.simplify(tol) for p in (land.geoms if hasattr(land, 'geoms') else [land])
             if p.area >= min_area]
    land2 = unary_union(polys)
    coast2 = [LineString(p.exterior.coords)
              for p in (land2.geoms if hasattr(land2, 'geoms') else [land2])]
    return land2, coast2, STRtree(coast2)


def encode_level_fast(level, y0, x0, land_local, coast, tree, coverage, sea=None):
    """Background records for every parcel of one directory that the coverage touches."""
    if sea is None:
        sea = SEA
    L = LEVELS[level]
    out = {}
    pl = prep(land_local)
    for row in range(L['rows']):
        for col in range(L['cols']):
            lon0, lat0, w, h = parcel_geo(level, y0, x0, row, col)
            rect = box(lon0, lat0, lon0 + w, lat0 + h)
            if rect.intersection(coverage).area <= 0:
                continue
            sea_polys, coastl, labels = [], [], []
            area_polys, extra_lines, extra_labels = [], [], []
            for poly, text in MARKERS['areas']:
                if not poly.intersects(rect):
                    continue
                inter = poly.intersection(rect)
                for p in (inter.geoms if hasattr(inter, 'geoms') else [inter]):
                    if p.geom_type == 'Polygon' and not p.is_empty:
                        shp = bg_encode.ring_shape(p.exterior, lon0, lat0, w, h, L['areatype'])
                        if shp:
                            area_polys.append(shp)
            for ln in MARKERS['lines']:
                if ln.intersects(rect):
                    extra_lines += bg_encode.line_shapes(ln.intersection(rect), lon0, lat0, w, h, L['coasttype'])
            for plon, plat, text in MARKERS['points']:
                if lon0 <= plon < lon0 + w and lat0 <= plat < lat0 + h:
                    extra_labels.append((0x4068, 0x4800, bg_encode.quant(plon, lon0, w),
                                         bg_encode.quant(plat, lat0, h), text))
            if not pl.intersects(rect):          # open sea
                sea_polys = [dict(type=L['seatype'], ref=0xffff,
                                  pts=bg_encode.densify([(0, 0), (0, 2047), (2047, 2047), (2047, 0), (0, 0)]))]
                labels = [(0x4089, 0x2800, 1024, 1024, sea)]
            elif pl.contains(rect):
                # All land. A header-only record, so that every parcel exists.
                out.setdefault(row, {})[col] = bg_encode.build_parcel_record(
                    level, y0, x0, row, col, [], extra_lines, extra_labels, area_polys=area_polys)
                continue
            else:
                water = rect.difference(land_local)
                polys = [p for p in (water.geoms if hasattr(water, 'geoms') else [water])
                         if p.geom_type == 'Polygon' and not p.is_empty]
                for p in polys:
                    s = bg_encode.ring_shape(p.exterior, lon0, lat0, w, h, L['seatype'])
                    if s:
                        sea_polys.append(s)
                pieces = []
                for i in tree.query(rect):
                    inter = coast[i].intersection(rect)
                    if inter.is_empty:
                        continue
                    pieces += [g for g in (inter.geoms if hasattr(inter, 'geoms') else [inter])
                               if g.geom_type == 'LineString']
                if pieces:
                    merged = linemerge(MultiLineString(pieces)) if len(pieces) > 1 else pieces[0]
                    coastl = bg_encode.line_shapes(merged, lon0, lat0, w, h, L['coasttype'])
                # A group holds at most 255 shapes: keep the largest.
                sea_polys = sorted(sea_polys, key=lambda s: -len(s['pts']))[:255]
                coastl = sorted(coastl, key=lambda s: -len(s['pts']))[:255]
                if polys:
                    big = max(polys, key=lambda p: p.area)
                    rp = big.representative_point()
                    labels = [(0x4089, 0x2800, bg_encode.quant(rp.x, lon0, w),
                               bg_encode.quant(rp.y, lat0, h), sea)]
            out.setdefault(row, {})[col] = bg_encode.build_parcel_record(
                level, y0, x0, row, col, sea_polys, coastl + extra_lines,
                labels + extra_labels, area_polys=area_polys)
    return out


def build_cell(name, land, coast, tree, template, bounds=None, data_version=None, road_section=True):
    pland = prep(land)
    yb, xb = tile_cell(name)
    cell = box(mesh_to_lon(xb), mesh_to_lat(yb), mesh_to_lon(xb + 8), mesh_to_lat(yb + 8))
    dirs = {2: [], 1: []}
    for level in (2, 1):
        n = LEVELS[level]['dir_meshes']
        for y0 in range(yb, yb + 8, n):
            for x0 in range(xb, xb + 8, n):
                area = box(mesh_to_lon(x0), mesh_to_lat(y0), mesh_to_lon(x0 + n), mesh_to_lat(y0 + n))
                land_local = land.intersection(area) if pland.intersects(area) else box(0, 0, 0, 0).buffer(0)
                recs = encode_level_fast(level, y0, x0, land_local, coast, tree, cell)
                if not recs:
                    continue
                D = bg_encode.make_dir(level, y0, x0, recs)
                D['_recs'] = recs
                dirs[level].append(D)
        parcels = sum(len(c) for D in dirs[level] for c in D['_recs'].values())
        print(f'  {name} level {level}: {len(dirs[level])} dirs, {parcels} parcels, {time.time()-t0:.0f}s')
    return bg_encode.assemble_tile(name, template, dirs, bounds or BOUNDS, data_version, road_section)


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    coast_path = P.coastline
    if '--coastline' in argv:
        i = argv.index('--coastline'); coast_path = argv[i + 1]; del argv[i:i + 2]
    version = None
    if '--data-version' in argv:
        i = argv.index('--data-version'); version = argv[i + 1]; del argv[i:i + 2]
    road_section = '--no-road-section' not in argv
    if not road_section:
        argv.remove('--no-road-section')
    if version is None:
        template, outdir, cells = argv[0], argv[1], argv[2:]
    else:
        template, outdir, cells = None, argv[0], argv[1:]
    cells = cells or P.cells
    if not cells:
        raise SystemExit('no cells: give them on the command line or in a profile')
    if not coast_path:
        raise SystemExit('no coastline: set source.coastline in the profile')
    land, coast, tree = load_land(coast_path)
    tmpl = open(template, 'rb').read() if template else None
    os.makedirs(outdir, exist_ok=True)
    for name in cells:
        tile = build_cell(name, land, coast, tree, tmpl, P.bounds, version, road_section)
        open(f'{outdir}/{name}.DAT', 'wb').write(tile)
        print(name, len(tile), 'bytes')


if __name__ == '__main__':
    main()
