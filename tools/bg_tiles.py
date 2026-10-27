#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background tiles (sea and land, levels 2 and 1) for a region's cells, from an OSM coastline. Roads go in afterwards with rd_build.py; lev_* tools reuse load_land, clean_land and encode_level_fast for levels 3-6."""
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
# extra features drawn into every level: areas (polygon, label), lines (LineString), points (lon, lat, text)
MARKERS = dict(areas=[], lines=[], points=[])
LANDUSE = {'items': [], 'tree': None}   # [(polygon, layer, class)] and an STRtree, set by load_landuse
LANDUSE_MIN = {1: 1e-7, 2: 4e-6}        # smallest land-use polygon drawn per level, in square degrees
DROPPED = dict(sea_polys=0, coastl=0, landuse=0, empty_sea_label=0)   # silent drops, counted for the build log

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


def load_landuse(path, classes=None):
    import json

    from shapely.geometry import shape
    items = []
    with open(path) as fh:
        feats = json.load(fh)['features']
    classes = {**clarion_bg.AREA_CLASSES, **(classes or {})}
    for f in feats:
        kind = f['properties']['kind']
        if kind not in classes:
            raise SystemExit('no area class for kind %r; pass --area-class %s=LAYER:CLASS' % (kind, kind))
        layer, cls = classes[kind]
        g = shape(f['geometry'])
        for p in (g.geoms if hasattr(g, 'geoms') else [g]):
            if p.geom_type == 'Polygon' and not p.is_empty:
                items.append((p, layer, cls))
    LANDUSE['items'] = items
    LANDUSE['tree'] = STRtree([p for p, _, _ in items]) if items else None
    print(f'land use {len(items)} polygons from {path}')


def landuse_layers(level, rect, lon0, lat0, w, h, land=None):
    """{layer: shapes} of the land use inside one parcel, outer rings only. land, when
    given, cuts woods and parks that run out over the sea to the parcel's actual land."""
    if LANDUSE['tree'] is None:
        return {}
    prefix = LEVELS[level]['seatype'] & 0xf000
    out = {}
    for i in LANDUSE['tree'].query(rect):
        poly, layer, cls = LANDUSE['items'][i]
        if not poly.intersects(rect):
            continue
        inter = poly.intersection(rect if land is None else land)
        for p in (inter.geoms if hasattr(inter, 'geoms') else [inter]):
            if p.geom_type != 'Polygon':
                continue
            if p.area < LANDUSE_MIN.get(level, 0):
                DROPPED['landuse'] += 1
                continue
            s = bg_encode.ring_shape(p.exterior, lon0, lat0, w, h, prefix | cls)
            if s:
                out.setdefault(layer, []).append(s)
    return out


def clean_land(land, level):
    """Land, coast lines and their index for one level; islands below the level's area
    floor are dropped and the rest simplified."""
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
            if not pl.intersects(rect):
                extra = {}
            else:
                extra = landuse_layers(level, rect, lon0, lat0, w, h,
                                       None if pl.contains(rect) else land_local.intersection(rect))
            if not pl.intersects(rect):          # open sea
                sea_polys = [dict(type=L['seatype'], ref=0xffff,
                                  pts=bg_encode.densify([(0, 0), (0, 2047), (2047, 2047), (2047, 0), (0, 0)]))]
                labels = [(0x4089, 0x2800, 1024, 1024, sea)]
                if not sea: DROPPED['empty_sea_label'] += 1
            elif pl.contains(rect):
                # all land: header-only record, so every parcel exists
                out.setdefault(row, {})[col] = bg_encode.build_parcel_record(
                    level, y0, x0, row, col, [], extra_lines, extra_labels, area_polys=area_polys, extra_layers=extra)
                continue
            else:
                water = rect.difference(land_local)
                polys = [p for p in (water.geoms if hasattr(water, 'geoms') else [water])
                         if p.geom_type == 'Polygon' and not p.is_empty]
                for p in polys:
                    for piece in bg_encode.no_holes(p):
                        s = bg_encode.ring_shape(piece.exterior, lon0, lat0, w, h, L['seatype'])
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
                if len(sea_polys) > 255: DROPPED['sea_polys'] += len(sea_polys) - 255
                if len(coastl) > 255: DROPPED['coastl'] += len(coastl) - 255
                sea_polys = sorted(sea_polys, key=lambda s: -len(s['pts']))[:255]
                coastl = sorted(coastl, key=lambda s: -len(s['pts']))[:255]
                if polys:
                    big = max(polys, key=lambda p: p.area)
                    rp = big.representative_point()
                    labels = [(0x4089, 0x2800, bg_encode.quant(rp.x, lon0, w),
                               bg_encode.quant(rp.y, lat0, h), sea)]
                    if not sea: DROPPED['empty_sea_label'] += 1
            out.setdefault(row, {})[col] = bg_encode.build_parcel_record(
                level, y0, x0, row, col, sea_polys, coastl + extra_lines,
                labels + extra_labels, area_polys=area_polys, extra_layers=extra)
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
    classes = {}
    while '--area-class' in argv:
        i = argv.index('--area-class'); kind, lc = argv[i + 1].split('='); del argv[i:i + 2]
        classes[kind] = (int(lc.split(':')[0]), int(lc.split(':')[1], 16))
    if '--landuse' in argv:
        i = argv.index('--landuse'); load_landuse(argv[i + 1], classes); del argv[i:i + 2]
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
    dropped = dict(DROPPED, oversize_rings=bg_encode.to_parcel.oversize, short_rings=bg_encode.ring_shape.dropped)
    if any(dropped.values()):
        print('dropped silently:', ', '.join(f'{k} {v}' for k, v in dropped.items() if v))


if __name__ == '__main__':
    main()
