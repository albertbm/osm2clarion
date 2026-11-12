#!/usr/bin/env python3
"""Build the capital-area POI card: the base map untouched, plus every real OSM POI in
the selected area (icon + POINT search facility), plus the x-first settlement fix
No whole-country towns here - that tooling is untouched.

--house-ranges mode swaps in per-link house-number ranges instead (--all-streets for
every selected-area street).

    build_poicap.py --config PATH --workdir WORKDIR --base IMG --out CARD.img
    build_poicap.py --help

Needs osm2clarion's pois tooling and a Python interpreter with osmium and shapely. The
base image is never modified. --config's poicap section holds the municipality lists,
settlement position fixes, the diacritic fold table and the short-ADDRE street list.
"""
import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time
import zlib

import cardcopy  # tools/ is this script's own directory

CARD_TOOLS = os.path.dirname(os.path.abspath(__file__))
BASE_IMG = OUT_IMG = PBF = None
O2C = O2C_ROOT = VENV_PY = FAT_TIME = CONFIG = None
AREA_MUNICIPALITIES = SETTLEMENTS = FOLD = ADDR_ONLY = POI_TILES = None


def load_config(path):
    global AREA_MUNICIPALITIES, SETTLEMENTS, FOLD, ADDR_ONLY, POI_TILES, CONFIG
    CONFIG = json.load(open(os.path.expanduser(path)))
    poicap = CONFIG['poicap']
    AREA_MUNICIPALITIES = poicap['municipalities']
    # settlement lat/lon words are stored as hex strings in config (JSON has no hex literals)
    SETTLEMENTS = {name: dict(la=int(v['la'], 16), lo=int(v['lo'], 16), x=v['x'], y=v['y'])
                    for name, v in poicap['settlements'].items()}
    FOLD = poicap['fold']
    ADDR_ONLY = poicap.get('addr_only_default')
    # a facility gets its refs and icon from the road tile it lies in, so every road tile
    POI_TILES = CONFIG.get('road_tiles') or [poicap['poi_tile']]
    return CONFIG


def restore_slots(out, old):
    """assemble() with no `extra=` clears the slot 8..299 pointer table (category 278,
    sub-category 279, nearby 15, ADM 280/287). Put each live pointer back shifted by
    however far assemble() moved the base structure - the name/tel frames compress to a
    different size whenever their content changes, so the boundary before the extra
    frames is not fixed even when nothing reorders them. Card 036 shipped with all five
    pointers at the default because this used to copy the old table unshifted."""
    old_extra_off = point_io.Point(old).extra_off
    delta = len(out) - old_extra_off
    out = bytearray(out)
    for i in range(8, 300):
        a, blocks = struct.unpack_from('>IH', old, 0x800 + i * 6)
        if a == 0xffffffff:
            continue
        struct.pack_into('>IH', out, 0x800 + i * 6,
                          point_io.addr(point_io.unaddr(a) + delta), blocks)
    return bytes(out)


POI_SLOTS = (15, 278, 279, 280, 287)


def _slot(plain, i):
    a, blocks = struct.unpack_from('>IH', plain, 0x800 + i * 6)
    if a == 0xffffffff:
        raise SystemExit('slot %d is empty - POI search would be disabled' % i)
    return point_io.unaddr(a), blocks


def _in_bounds(off, size, plain_len, extra_off, what):
    if off < extra_off or off + size > plain_len:
        raise SystemExit('%s lands outside the file (off=0x%x, size=0x%x)' % (what, off, size))


def check_poi_slots(plain):
    """Fail loudly if any POI search slot is empty or points at something unparseable,
    rather than ship a card where every POI keyboard greys out silently (card 036's bug):
    walk slot 15's nearby table and decode slots 278/279 through point_io.category_table,
    rather than just checking the pointers are non-default."""
    p = point_io.Point(plain)
    for i in (280, 287):
        off, blocks = _slot(plain, i)
        _in_bounds(off, blocks * 0x100, len(plain), p.extra_off, 'ADM slot %d' % i)

    off, blocks = _slot(plain, 15)
    o = off
    n = 0
    while True:
        next_hw, child_a, size_w = struct.unpack_from('>HII', plain, o)
        child_off = point_io.unaddr(child_a)
        _in_bounds(child_off, size_w * 2, len(plain), p.extra_off, 'slot 15 nearby block %d' % n)
        n += 1
        if next_hw == 0xffff:
            break
        o += 28
    if n == 0:
        raise SystemExit('slot 15 has no category entries')

    for i in (278, 279):
        entries = point_io.category_table(plain, i)
        if not entries:
            raise SystemExit('slot %d decodes to no entries' % i)
        for e in entries:
            _in_bounds(e['trie_at'], 1, len(plain), p.extra_off, 'slot %d trie' % i)
            _in_bounds(e['rec_at'], e['nrec'] * point_io.REC, len(plain), p.extra_off, 'slot %d records' % i)
            if e['nrec'] <= 0:
                raise SystemExit('slot %d entry with no records' % i)


def fix_settlements(plain):
    """Rewrite the 8 settlement rows x-first, GRINDAVÍK also onto its correct parcel."""
    p = point_io.Point(plain)
    names = list(p.names)
    touched = {}
    for i, nr in enumerate(names):
        f = point_io.parse_name_record(nr)
        if f['category'] != point_io.SETTLEMENT or f['name'] not in SETTLEMENTS:
            continue
        fx = SETTLEMENTS[f['name']]
        nf = dict(f)
        nf['lat'], nf['lon'], nf['x'], nf['y'] = fx['la'], fx['lo'], fx['x'], fx['y']
        if (f['lat'], f['lon']) != (fx['la'], fx['lo']):
            nf['glat'], nf['glon'] = fx['la'], fx['lo']
        names[i] = point_io.name_record(**nf)
        touched[i] = f['name']
    assert len(touched) == 8, touched
    recs = []
    for i in range(p.nrec):
        r = p.rec(i)
        d = point_io.parse_data_record(r)
        if d['name_idx'] in touched:
            fx = SETTLEMENTS[touched[d['name_idx']]]
            nd = dict(d, nlevel=p.nlevel, levels=d['levels'][:p.nlevel])
            nd['lat'], nd['lon'], nd['x'], nd['y'] = fx['la'], fx['lo'], fx['x'], fx['y']
            recs.append(point_io.data_record(**nd))
        else:
            recs.append(r)
    out, _, _ = point_io.assemble(p.prefix, p.df, p.keys, recs, names, list(p.tels),
                                   p.cr, p.kw, p.area, p.nlevel)
    out = restore_slots(out, plain)
    full = out + p.extra
    assert len(full) == len(plain)
    return full


def extract_and_filter(workdir, areas):
    pois_json = os.path.join(workdir, 'pois.json')
    subprocess.run([VENV_PY, os.path.join(O2C, 'extract_pois.py'), PBF, pois_json], check=True)

    import pickle
    import osmium
    import osmium.geom
    from shapely import wkb, prepare
    from shapely.geometry import Point as SPoint

    fab = osmium.geom.WKBFactory()
    municipalities = [m for area in areas for m in AREA_MUNICIPALITIES[area]]
    found = {}

    class H(osmium.SimpleHandler):
        def area(self, a):
            tags = {t.k: t.v for t in a.tags}
            if tags.get('boundary') != 'administrative' or tags.get('admin_level') != '6':
                return
            if tags.get('name') in municipalities:
                found[tags['name']] = wkb.loads(fab.create_multipolygon(a))
    H().apply_file(PBF, locations=True)
    assert sorted(found) == sorted(municipalities), (sorted(found), municipalities)
    for poly in found.values():
        prepare(poly)

    pois = json.load(open(pois_json))
    out = []
    for p in pois:
        pt = SPoint(p['lon'], p['lat'])
        for name, poly in found.items():
            if poly.contains(pt):
                out.append(dict(p, muni=name))
                break
    scope = 'capital' if areas == ['capital'] else 'area'
    area_json = os.path.join(workdir, 'pois_%s.json' % scope)
    json.dump(out, open(area_json, 'w'), ensure_ascii=False)
    print('%s POIs: %d of %d' % (scope, len(out), len(pois)))
    return area_json, found


def run_poi_point(point_in, pois_json, road_tiles, point_out, allow_placeholder_refs=False):
    cmd = [VENV_PY, os.path.join(O2C, 'poi_point.py'), point_in, pois_json, point_out]
    for t in road_tiles:
        cmd += ['--road-tile', t]
    cmd += ['--municipalities', PBF, '--fold', FOLD, '--allow-missing-ladder']
    if allow_placeholder_refs:
        cmd.append('--allow-placeholder-refs')
    subprocess.run(cmd, check=True)


def real_latlon(f):
    row, col = cb.parcel_rowcol(1, f['lat'], f['lon'])
    return (row + f['y'] / 2048) / 48, (col + f['x'] / 2048) / 32 - 924


def fix_orphan_ladders(plain, municipalities, pois):
    """Reassign root-ladder facilities in municipalities with no settlement point to
    their nearest settlement by straight-line distance, and say how far that is."""
    p = point_io.Point(plain)
    towns = [(point_io.parse_name_record(nr)) for nr in p.names
             if point_io.parse_name_record(nr)['category'] == point_io.SETTLEMENT]
    town_pos = [(t['name'], t['levels']) + real_latlon(t) for t in towns]
    settled = set()
    from shapely.geometry import Point as SPoint
    for t in towns:
        lat, lon = real_latlon(t)
        for muni, poly in municipalities.items():
            if poly.contains(SPoint(lon, lat)):
                settled.add(muni)
                break
    orphaned = {name: poly for name, poly in municipalities.items() if name not in settled}
    print('  municipalities with no settlement: %s' %
          (', '.join(sorted(orphaned)) if orphaned else 'none'))

    names = list(p.names)
    touched = {}
    first = len(names) - len(pois)
    for i, poi in zip(range(first, len(names)), pois):
        nr = names[i]
        f = point_io.parse_name_record(nr)
        assert f['name'] == poi['name'], (f['name'], poi['name'])
        if poi['muni'] not in orphaned or tuple(f['levels']) != (p.area, 0, 0):
            continue
        lat, lon = real_latlon(f)
        nearest = min(town_pos, key=lambda t: math.hypot(t[2] - lat, (t[3] - lon) * math.cos(math.radians(lat))))
        dist_km = math.hypot((nearest[2] - lat) * 111.32, (nearest[3] - lon) * 111.32 * math.cos(math.radians(lat)))
        print('  %-20s -> %s ladder %s, %.1f km' % (f['name'], nearest[0], nearest[1], dist_km))
        nf = dict(f, levels=nearest[1])
        names[i] = point_io.name_record(**nf)
        touched[i] = nearest[1]
    recs = []
    for i in range(p.nrec):
        r = p.rec(i)
        d = point_io.parse_data_record(r)
        if d['name_idx'] in touched:
            nd = dict(d, nlevel=p.nlevel, levels=touched[d['name_idx']])
            recs.append(point_io.data_record(**nd))
        else:
            recs.append(r)
    out, _, _ = point_io.assemble(p.prefix, p.df, p.keys, recs, names, list(p.tels),
                                   p.cr, p.kw, p.area, p.nlevel)
    out = restore_slots(out, plain)
    full = out + p.extra
    assert len(full) == len(plain)
    return full


def add_icons(tile_plains, pois_json, allow_placeholder_refs=False):
    """tile_plains maps a tile name to its bytes (our own built road tiles only); returns
    the tiles that gained an icon, rebuilt."""
    tiles = {name: clarion_tile.read_tile(d) for name, d in tile_plains.items()}
    pois = json.load(open(pois_json))

    def plain_of(r):
        return zlib.decompress(r['raw']) if r['raw'][:2] == b'\x58\x85' else r['raw']

    idx = {}
    for name, t in tiles.items():
        for sec in t['sections']:
            if sec['id'] != 1:
                continue
            for L in sec['levels']:
                if L['level'] not in (1, 2):
                    continue
                for D in L['dirs']:
                    for row, sl in D['slots'].items():
                        for i, r in enumerate(sl['recs']):
                            if r['k'] % 2 == 0:
                                continue
                            pl = plain_of(r)
                            if len(pl) < 4:
                                continue
                            w0, w1 = struct.unpack_from('>HH', pl, 0)
                            idx[(L['level'], w0, w1)] = (name, sl, i, r)

    groups = {}
    missing = []
    for poi in pois:
        code = 0x2000 | (poi['sub'] if poi['sub'] != 0xffff else cb.ICON_CODES[poi['cat']])
        for level in (1, 2):
            row, col, x, y = cb.parcel_of(level, poi['lat'], poi['lon'])
            w0, w1 = cb.parcel_code(level, row, col)
            if (level, w0, w1) not in idx:
                missing.append((poi['name'], level, w0, w1))
                continue
            groups.setdefault((level, w0, w1), []).append((code, x, y))
    if missing:
        if not allow_placeholder_refs:
            raise SystemExit('facility icons have no background parcels:\n%s' % '\n'.join(
                '  %s: level %d parcel %04x/%04x' % item for item in missing))
        print('  %d facility/level pairs have no background record, skipped' % len(missing))

    touched = set()
    for (level, w0, w1), pts in groups.items():
        name, sl, i, r = idx[(level, w0, w1)]
        touched.add(name)
        rec = cb.parse_record(plain_of(r))
        for code, x, y in pts:
            side = struct.pack('>HHHHHH', 0, 0, 0xffff, 0, code, 0)
            cb.add_point(rec, 0x00ff, x, y, side=side)
        new_plain = cb.build_record(rec)
        c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
        comp = c.compress(new_plain) + c.flush()
        if len(comp) % 2:
            comp += b'\0'
        r['raw'] = comp
        r['words'] = len(new_plain) // 2
        body = bytearray(sl['body'])
        struct.pack_into('>II', body, 8 * r['k'], len(comp) // 2, len(new_plain) // 2)
        sl['body'] = bytes(body)
    print('icons: %d parcels touched in %s' % (len(groups), ', '.join(sorted(touched)) or 'no tile'))
    return {name: clarion_tile.write_tile(tiles[name]) for name in sorted(touched)}


def build_house_ranges(workdir, args):
    """Card 037/038: the base card untouched except ADDRE001, rebuilt with per-link
    house-number ranges. Everything else stays whatever the base card already carries."""
    run = lambda *a: subprocess.run(a, check=True)
    cardcopy.clone(BASE_IMG, OUT_IMG)

    addr_dir = os.path.join(workdir, 'addr')
    os.makedirs(addr_dir, exist_ok=True)
    env = dict(os.environ, OSM2CLARION=O2C_ROOT,
               O2C_CONFIG=os.path.abspath(os.path.expanduser(args.config)))
    build_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'addr', 'build_addr_area0.py')
    cmd = [VENV_PY, build_script, addr_dir, '--shapes'] + args.shapes + [
        '--area', args.area, '--house-ranges', args.house_ranges]
    if args.area != 'capital':
        print('warning: only ADDRE001 and ZCODE001 are replaced; POINT001 remains from the base card')
    if not args.all_streets and args.area == 'capital':
        if not ADDR_ONLY:
            raise SystemExit('without --all-streets this needs [poicap] addr_only_default')
        cmd += ['--only', ADDR_ONLY]
    if args.no_ranges_town:
        cmd += ['--no-ranges-town', args.no_ranges_town]
    if args.addresses:
        cmd += ['--addresses', args.addresses]
    if args.postcodes:
        cmd += ['--postcodes', args.postcodes]
    subprocess.run(cmd, check=True, env=env)

    plain = open(os.path.join(addr_dir, 'ADDRE001.DAT'), 'rb').read()
    zplain = open(os.path.join(addr_dir, 'ZCODE001.DAT'), 'rb').read()
    writes = []
    for name, data in (('ADDRE001', plain), ('ZCODE001', zplain)):
        c, i = wrap(data)
        for ext, blob in (('cmp', c), ('ind', i)):
            path = os.path.join(workdir, '%s.out.DAT.%s' % (name, ext))
            open(path, 'wb').write(blob)
            writes.append('REFER001:%s.DAT.%s=%s' % (name, ext, path))
    cmp_out, ind_out = wrap(plain)

    run(sys.executable, os.path.join(O2C, 'fatedit.py'), OUT_IMG, '--write', *writes)
    run(sys.executable, os.path.join(O2C, 'drstr_refresh.py'), OUT_IMG,
        'REFER001:ADDRE001.DAT=%d' % len(plain), 'REFER001:ZCODE001.DAT=%d' % len(zplain))

    if FAT_TIME is not None:
        from fatedit import Editor
        from fatwrite import fat_time
        ht, hd = fat_time(FAT_TIME)
        editor = Editor(OUT_IMG)
        for dirname, name in (('', 'DRSTR001.DAT'), ('REFER001', 'ADDRE001.DAT.cmp'), ('REFER001', 'ADDRE001.DAT.ind'),
                              ('REFER001', 'ZCODE001.DAT.cmp'), ('REFER001', 'ZCODE001.DAT.ind')):
            entry = editor.find(dirname, name)
            entry.short[18:20], entry.short[22:24], entry.short[24:26] = hd, ht, hd
        editor.close()

    print('ADDRE001 %d B plain (cmp %d, ind %d)' % (len(plain), len(cmp_out), len(ind_out)))
    print('wrote', OUT_IMG)


def main(workdir, args=None):
    os.makedirs(workdir, exist_ok=True)
    run = lambda *a: subprocess.run(a, check=True)

    if args is not None and args.house_ranges:
        return build_house_ranges(workdir, args)

    cardcopy.clone(BASE_IMG, OUT_IMG)

    point_cmp = os.path.join(workdir, 'POINT001.DAT.cmp')
    point_ind = os.path.join(workdir, 'POINT001.DAT.ind')
    tiles_in = {t: os.path.join(workdir, t + '.DAT') for t in POI_TILES}
    run(sys.executable, os.path.join(CARD_TOOLS, 'extract.py'), BASE_IMG, 'REFER001/POINT001.DAT.cmp', point_cmp)
    run(sys.executable, os.path.join(CARD_TOOLS, 'extract.py'), BASE_IMG, 'REFER001/POINT001.DAT.ind', point_ind)
    for t, path in tiles_in.items():
        run(sys.executable, os.path.join(CARD_TOOLS, 'extract.py'), BASE_IMG, 'MAPAL001/%s.DAT' % t, path)

    from cmp_wrap import wrap as _wrap  # noqa: F811
    ind = open(point_ind, 'rb').read()
    unc, comp, nblocks, blksz = struct.unpack_from('<IIII', ind, 0)
    offs = list(struct.unpack_from('<%dI' % nblocks, ind, 16)) + [None]
    data = open(point_cmp, 'rb').read()
    offs[-1] = len(data)
    plain = b''.join(zlib.decompress(data[offs[i]:offs[i + 1]]) for i in range(nblocks))
    assert len(plain) == unc

    print('settlement fix:')
    plain = fix_settlements(plain)

    print('POI extract + filter:')
    pois_json, munis = extract_and_filter(workdir, args.areas)

    print('poi_point.py:')
    plain_path = os.path.join(workdir, 'POINT001.plain.DAT')
    open(plain_path, 'wb').write(plain)
    merged_path = os.path.join(workdir, 'POINT001.merged.DAT')
    run_poi_point(plain_path, pois_json, list(tiles_in.values()), merged_path,
                  getattr(args, 'allow_placeholder_refs', False))

    print('orphan municipality ladder override:')
    merged = open(merged_path, 'rb').read()
    pois = json.load(open(pois_json))
    final_plain = fix_orphan_ladders(merged, munis, pois)

    print('POI slot check:')
    check_poi_slots(final_plain)

    print('icons:')
    new_tiles = add_icons({t: open(p, 'rb').read() for t, p in tiles_in.items()}, pois_json,
                          getattr(args, 'allow_placeholder_refs', False))

    final_cmp, final_ind = wrap(final_plain)
    tiles_out = {}
    for t, data in new_tiles.items():
        tiles_out[t] = os.path.join(workdir, t + '.out.DAT')
        open(tiles_out[t], 'wb').write(data)
    cmp_out = os.path.join(workdir, 'POINT001.out.DAT.cmp')
    ind_out = os.path.join(workdir, 'POINT001.out.DAT.ind')
    open(cmp_out, 'wb').write(final_cmp)
    open(ind_out, 'wb').write(final_ind)

    run(sys.executable, os.path.join(O2C, 'fatedit.py'), OUT_IMG, '--write',
        *['MAPAL001:%s.DAT=%s' % item for item in tiles_out.items()],
        'REFER001:POINT001.DAT.cmp=%s' % cmp_out,
        'REFER001:POINT001.DAT.ind=%s' % ind_out)
    run(sys.executable, os.path.join(O2C, 'drstr_refresh.py'), OUT_IMG,
        *['MAPAL001:%s.DAT' % t for t in tiles_out], 'REFER001:POINT001.DAT=%d' % len(final_plain))

    if FAT_TIME is not None:
        from fatedit import Editor
        from fatwrite import fat_time
        ht, hd = fat_time(FAT_TIME)
        editor = Editor(OUT_IMG)
        for dirname, name in ((('', 'DRSTR001.DAT'),) + tuple(('MAPAL001', t + '.DAT') for t in tiles_out)
                              + (('REFER001', 'POINT001.DAT.cmp'), ('REFER001', 'POINT001.DAT.ind'))):
            entry = editor.find(dirname, name)
            entry.short[18:20], entry.short[22:24], entry.short[24:26] = hd, ht, hd
        editor.close()

    print('wrote', OUT_IMG)


def parser():
    p = argparse.ArgumentParser(description='Build a POI/house-range card image from a base card.')
    p.add_argument('--config', default=os.environ.get('O2C_CONFIG'),
                   metavar='PATH', help='build config (default: $O2C_CONFIG)')
    p.add_argument('--osm2clarion', default=os.path.dirname(CARD_TOOLS),
                   metavar='DIR', help='osm2clarion checkout (default: this checkout)')
    p.add_argument('--python', metavar='PATH', help='Python interpreter with osmium (default: this interpreter)')
    p.add_argument('--base', required=True, metavar='IMG', help='read-only source card')
    p.add_argument('--out', required=True, metavar='IMG', help='output card image')
    p.add_argument('--pbf', metavar='FILE', help='OpenStreetMap PBF input (default: config data.pbf)')
    p.add_argument('--fat-time', metavar='YYYY-MM-DD HH:MM:SS', help='fixed FAT timestamp for reproducible images')
    p.add_argument('--workdir', required=True, metavar='DIR', help='directory for intermediate files')
    p.add_argument('--allow-placeholder-refs', action='store_true',
                   help='allow placeholder road references and skip icons with no background parcel')
    p.add_argument('--area', default='capital', metavar='NAME[,NAME...]',
                   help='named municipality areas to include (default: capital)')
    p.add_argument('--house-ranges', metavar='STREET[,STREET...]',
                   help='build card 037 instead: swap in an ADDRE with per-link house number '
                        'ranges for these streets (upper case), or ALL, everything else untouched')
    p.add_argument('--no-ranges-town', default='', metavar='TOWN[,TOWN...]',
                   help='--house-ranges ALL: these towns keep the generic street row')
    p.add_argument('--all-streets', action='store_true',
                   help='--house-ranges: keep every selected-area street in ADDRE')
    p.add_argument('--shapes', nargs='+', metavar='FILE', help='--house-ranges: shape index files '
                   '(rd_build.py --shape-index), same ones the base card was addressed from')
    p.add_argument('--addresses', metavar='FILE', help='--house-ranges: Stadfangaskra.csv override')
    p.add_argument('--postcodes', metavar='FILE', help='--house-ranges: postnumeraskra_2024.csv override')
    return p


def configure(args, p):
    global O2C, O2C_ROOT, VENV_PY, BASE_IMG, OUT_IMG, PBF, FAT_TIME, point_io, cb, clarion_tile, wrap
    if not args.config:
        p.error('--config PATH is required (or set $O2C_CONFIG)')
    config = load_config(args.config)
    args.areas = args.area.split(',')
    unknown = [a for a in args.areas if a not in AREA_MUNICIPALITIES]
    if unknown:
        p.error('unknown --area: %s' % ', '.join(unknown))
    if not args.pbf:
        args.pbf = config['data']['pbf']
    root = os.path.abspath(os.path.expanduser(args.osm2clarion))
    O2C = os.path.join(root, 'tools')
    O2C_ROOT = root
    required_tools = (('point_io.py', 'clarion_bg.py', 'clarion_tile.py', 'fatedit.py', 'drstr_refresh.py',
                        'cmp_wrap.py') if args.house_ranges else
                       ('extract_pois.py', 'poi_point.py', 'point_io.py', 'clarion_bg.py', 'clarion_tile.py',
                        'fatedit.py', 'drstr_refresh.py', 'cmp_wrap.py'))
    missing = [name for name in required_tools if not os.path.isfile(os.path.join(O2C, name))]
    if missing:
        p.error('--osm2clarion %s is missing tools/%s' % (root, ', tools/'.join(missing)))
    if args.house_ranges and not args.shapes:
        p.error('--house-ranges needs --shapes')
    if args.all_streets and not args.house_ranges:
        p.error('--all-streets needs --house-ranges')
    VENV_PY = os.path.abspath(os.path.expanduser(args.python)) if args.python else sys.executable
    if not os.path.isfile(VENV_PY) or not os.access(VENV_PY, os.X_OK):
        p.error('--python is not an executable file: %s' % VENV_PY)
    missing_modules = []
    for module in ('osmium', 'shapely'):
        probe = subprocess.run([VENV_PY, '-c', 'import ' + module], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if probe.returncode:
            missing_modules.append(module)
    if missing_modules:
        p.error('%s cannot import %s; pass --python PATH for a suitable interpreter'
                % (VENV_PY, ' and '.join(missing_modules)))
    BASE_IMG = os.path.abspath(os.path.expanduser(args.base))
    OUT_IMG = os.path.abspath(os.path.expanduser(args.out))
    PBF = os.path.abspath(os.path.expanduser(args.pbf))
    if args.fat_time:
        try:
            FAT_TIME = time.mktime(time.strptime(args.fat_time, '%Y-%m-%d %H:%M:%S'))
        except ValueError:
            p.error('--fat-time must be YYYY-MM-DD HH:MM:SS')
    for label, path in (('--base', BASE_IMG), ('--pbf', PBF)):
        if not os.path.isfile(path):
            p.error('%s file does not exist: %s' % (label, path))
    if not os.path.isdir(CARD_TOOLS):
        p.error('card tools directory does not exist: %s' % CARD_TOOLS)
    out_parent = os.path.dirname(OUT_IMG)
    if not os.path.isdir(out_parent):
        p.error('--out parent directory does not exist: %s' % out_parent)
    sys.path.insert(0, O2C)
    sys.path.insert(0, CARD_TOOLS)
    import point_io as _point_io, clarion_bg as _cb, clarion_tile as _clarion_tile  # noqa: E402
    from cmp_wrap import wrap as _wrap  # noqa: E402
    point_io, cb, clarion_tile, wrap = _point_io, _cb, _clarion_tile, _wrap
    return os.path.abspath(os.path.expanduser(args.workdir))


if __name__ == '__main__':
    arg_parser = parser()
    options = arg_parser.parse_args()
    if options.python:
        selected = os.path.abspath(os.path.expanduser(options.python))
        if not os.path.isfile(selected) or not os.access(selected, os.X_OK):
            arg_parser.error('--python is not an executable file: %s' % selected)
        if selected != os.path.abspath(sys.executable):
            os.execv(selected, [selected, os.path.abspath(__file__)] + sys.argv[1:])
    main(configure(options, arg_parser), options)
