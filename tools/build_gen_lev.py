#!/usr/bin/env python3
"""Build the generated map files, put them on a fresh copy of the unit's base card, and write
the card's manifest.

Each step runs its command into BUILD_DIR. The card then gets the generated LEV3456R.DAT (old clusters zeroed),
its DRSTR size, MMAIN's level counts summed from every MAPAL001 header, and a CMP.LST made
from the card's own .cmp files. The tiles and address files already on the card are not
rewritten: the manifest checks them against what their steps produce now.

The manifest covers every file in MAPAL001, REFER001, REFER002, HOUSE001 and the root index files.
Each is one of:

    generated   byte-identical to a step's output; the step's command and inputs are listed
    edited      the base card's file with named fields rewritten by this build
    leftover    no step produced it

    build_gen_lev.py --config PATH --unit NAME --slug NAME BUILD_DIR [--base IMG] [--what TEXT]
                     [--coast PATH] [--reuse] [--number N] [--partial]
                     [--map-from CARD | --restamp-from CARD]
                     [--roads [--also NAME,NAME] [--to NAME,NAME [--by time]] | --area NAME[,NAME...] [--coarse-graph]]
                     [--write lev|tiles,addr] [--addr-only NAME,NAME]
                     [--landuse lon0,lat0,lon1,lat1 [--landuse-kinds forest,water,...] [--buildings LAYER:CLASS]
                      [--house]]

--config names the build's config file (unit table, areas, cells, data paths, card naming -
build.py writes it from the profile). Every build starts from a fresh cp -c of --base, which defaults to
the unit's base card (config's card_naming.base_pattern). --unit picks the DATA VERSION every
generated file carries (config units.*.version; the firmware checks it against the NAND) and
the card build_icode_clean.py reads its two ICODE001.DAT files from (config units.*.icode). The
card is written under CARDS/builds following card_naming.build_pattern, with its manifest
beside it, one past the highest existing number, or the same number when the newest build is
this slug on another unit (twins share a number), or the number --number gives. A line goes to the top of the cards log
(config cards_log), status untested; --what is its "what changed" text (default: the slug).
--reuse skips steps whose outputs exist. --partial writes the card and its manifest with a
.partial suffix and leaves out the log line; build.py renames them once its check step passes.

--map-from CARD writes the map, address and POI files of a finished card onto this unit's base, as
they are: nothing is generated, restamped or recompressed, so the source must carry this unit's
DATA VERSION (a card for another version is refused; --restamp-from makes that one), and the ICODE
files that number its towns go with them. The target keeps its own app and
settings and gets a refreshed index. This is "build once, write per unit"; BUILD_DIR is only a
place for the config.

--restamp-from CARD does the same with this unit's DATA VERSION: every stamp in those files is
rewritten (packed files are unpacked, stamped and repacked) and the icode step runs on this
unit's ICODE input. That is all that differs between the units' full builds, so one full build
gives the others' cards.

--roads adds the road steps: gen_roads_path.py picks the ways (--also adds whole streets,
--to a route to each named street and the street itself, --by how that route is weighed),
rd_build.py writes them into step 1's tiles, names in upper case, with a shape index per tile,
sec4_write.py gives the config's sec4_cells their section 4, and the address step points its
streets at the index. The card's tiles are then the rd step's, not bg_tiles'. --write names
what assemble puts on the card (default lev); the rest is only checked against the card.

--area builds named areas instead of a path: every drivable public road in their admin_level 6
relations (config areas) into the areas' road cells, all given section 4; coarse roads at
levels 3 to 5 in LEV3456R.DAT; and every address of those municipalities out of config's
address CSV (config data.addresses/postcodes, addr_csv.py).
--addr-only NAME,NAME keeps only those streets in ADDRE (towns and postcodes stay whole).

--landuse adds a step that cuts OSM land use (forest, park, lakes; extract_landuse.py) to the
box, and step 1 draws it into the tiles under the roads. --landuse capital names that area's
box; --landuse area uses the union selected by --area.
--landuse-kinds narrows the kinds.
--buildings LAYER:CLASS adds OSM building footprints in that section-1 layer and class (a trial:
no EUR class for buildings is known, see REPORT-buildings.md).
--house writes the landuse box as HOUSE001 files, the level-0 map the unit draws at its
building-scale zoom: merged building blocks, green areas, water and the sea, and buffered road
polygons (house_encode.py, REPORT-building-scale.md); per-file counts land in house/stats.json.
Only cells inside a populated area are written (a place node buffered --house-city-buffer
metres, default 400, or a residential/commercial/industrial/retail landuse polygon buffered
--house-landuse-buffer metres, default 100; housearea/PLAN.md). Off by default.
"""
import fnmatch
import hashlib
import json
import os
import re
import struct
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OSM = HERE                            # sibling format tools (rd_build.py, bg_tiles.py, ...)
OSM_ROOT = os.path.dirname(HERE)      # this checkout's root (profiles/, git history)
sys.path.insert(0, HERE)


def _config_path():
    argv = sys.argv[1:]
    if '--config' in argv:
        return argv[argv.index('--config') + 1]
    path = os.environ.get('O2C_CONFIG')
    if not path:
        raise SystemExit('build_gen_lev.py needs --config PATH (or $O2C_CONFIG)')
    return path


from card_pack import ed_read, mmain_card, restamped, wrap  # noqa: E402
from drstr_add import add as drstr_add, children, dir_records, remove as drstr_remove  # noqa: E402
from drstr_sizes import set_sizes  # noqa: E402
from fatedit import Editor  # noqa: E402
import fatread  # noqa: E402
import buildstamp  # noqa: E402
import cards_log  # noqa: E402
from card_scope import SCOPE, card_files, cmp_lst, drstr_check  # noqa: E402

_CONFIG_FILE = os.path.abspath(os.path.expanduser(_config_path()))
CONFIG = json.load(open(_CONFIG_FILE))
REPO = os.path.dirname(os.path.dirname(_CONFIG_FILE))   # the country repo: <repo>/config/NAME.json
CARDS = os.environ.get('CARDS_DIR', os.path.expanduser('~/cards'))  # override for scratch test builds
CELLS = CONFIG['cells']
VERSION = next(iter(CONFIG['units'].values()))['version']
UNITS = {u: dict(version=c['version'],
                  icode=c['icode'].replace('$CARDS', CARDS) if '$CARDS' in c['icode']
                        else os.path.expanduser(c['icode']))
          for u, c in CONFIG['units'].items()}
CARD_PREFIX = CONFIG['card_naming']['prefix']
CARD_BUILD_PATTERN = CONFIG['card_naming']['build_pattern']
CARDS_LOG = CONFIG['cards_log']
NAME = re.compile(r'%s-(\d{3})-(%s)-(.+)\.img$' % (CARD_PREFIX, '|'.join(CONFIG['card_naming']['units'])))
PACKED = ['ADDRE001.DAT', 'ZCODE001.DAT', 'POINT001.DAT', 'INTER001.DAT', 'COORD001.DAT']
SEC4_CELLS = CONFIG['sec4_cells']     # the tiles that carry roads get the build's section 4
AREAS = {name: dict(relations=a['relations'], cells=a['cells'], bbox=tuple(a['bbox']))
          for name, a in CONFIG['areas'].items()}
# A HOUSE001 parcel whose buildings pass this many vertices gets the coarser blocks; the
# busiest Grafarholt parcel of build 010 has 1,790 vertices in all layers
HOUSE_DENSE = 900
ADDR_ONLY = None      # --addr-only: the streets ADDRE keeps (build_addr_area0.py --only)
PBF = os.path.expanduser(CONFIG['data']['pbf'])
PROFILE = CONFIG['profile']           # the profile file build.py read, which the format tools read too
ADDRESSES = os.path.expanduser(CONFIG['data']['addresses'])
POSTCODES = os.path.expanduser(CONFIG['data']['postcodes'])


sha = buildstamp.sha
sha_file = buildstamp.sha_file


def area_values(value):
    names = value.split(',')
    assert names and all(n in AREAS for n in names), value
    relations = [r for n in names for r in AREAS[n]['relations']]
    cells = [c for c in CELLS if any(c in AREAS[n]['cells'] for n in names)]
    boxes = [AREAS[n]['bbox'] for n in names]
    bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
    return relations, cells, bbox


def box_text(box):
    return ','.join(str(x) for x in box)


def git_rev(path):
    try:
        rev = subprocess.run(['git', '-C', path, 'rev-parse', '--short', 'HEAD'],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', path, 'status', '--porcelain'],
                               capture_output=True, text=True).stdout.strip()
        return rev + ('+dirty' if dirty else '')
    except (OSError, subprocess.CalledProcessError):
        return 'not a git repository'


def extract(img, path, dest):
    """Copy one file off a card image, read-only, or from a directory of saved inputs."""
    if os.path.isdir(img):
        shutil.copyfile(os.path.join(img, path.replace('/', '_')), dest)
        return
    with fatread.Fat32(img) as fs:
        for p, sz, c, _ in fs.walk():
            if sz is not None and p.strip('/') == path:
                data = fs.f.read_clusters(c, sz)
                with open(dest, 'wb') as f:
                    f.write(data)
                return
    raise SystemExit('%s not on %s' % (path, img))


def road_steps(B, also, to=None, by=None, area=None):
    """Ways, then rd_build into step 1's tiles, then section 4: the card's tiles."""
    t = OSM; R = os.path.join(B, 'rd')
    turns_all = osm_paths(B)['turns']
    roads, turns = B + '/roads/roads.json', B + '/roads/turns.json'
    if area:
        relations, sec4, _ = area_values(area)
        sel = ['uv', 'run', '--with', 'osmium', '--with', 'shapely', 'python', t + '/osm_roads.py', PBF, roads,
               '--public', '--relations', ','.join(map(str, relations)), '--turns', turns_all, turns, '--ramp-names']
        seltool = t + '/osm_roads.py'
    else:
        sel = ['uv', 'run', '--with', 'osmium', 'python', HERE + '/gen_roads_path.py', PBF, turns_all, roads, turns]
        seltool = HERE + '/gen_roads_path.py'
    sec4 = sec4 if area else SEC4_CELLS
    cmds = []
    for c in CELLS:
        dest = R + '/%s.DAT' % c if c not in sec4 else R + '/%s.nosec4.DAT' % c
        cmds.append([sys.executable, t + '/rd_build.py', '--profile', PROFILE, '--simplify', '2',
                     '--l2-graph', '--upper-names', '--turn-restrictions', turns, B + '/tiles/%s.DAT' % c, dest, c, roads,
                     '--shape-index', R + '/%s.shapes.json' % c])
        if c in sec4:
            cmds.append([sys.executable, t + '/sec4_write.py', dest, R + '/%s.DAT' % c, '--country', '352'])
            cmds.append([sys.executable, t + '/shape_index.py', 'verify', R + '/%s.shapes.json' % c, R + '/%s.DAT' % c])
    return [
        dict(name='roads', cwd=REPO, cmds=[sel + (['--also', also] if also else []) + (['--to', to] if to else [])
                                           + (['--by', by] if by else [])],
             inputs=[PBF, turns_all], tools=[seltool],
             products=[roads, turns], outputs={}),
        dict(name='rd', cwd=REPO, cmds=cmds,
             inputs=[roads, turns, PROFILE] + [B + '/tiles/%s.DAT' % c for c in CELLS],
             tools=[t + '/rd_build.py', t + '/rd_encode.py', t + '/fa_encode.py', t + '/shape_index.py',
                    t + '/clarion_tile.py', t + '/clarion_bg.py', t + '/sec4_write.py'],
             products=[R + '/%s.shapes.json' % c for c in CELLS],
             outputs={'MAPAL001/%s.DAT' % c: ('file', R + '/%s.DAT' % c) for c in CELLS}),
    ]


HOUSE_CITY_BUFFER, HOUSE_LANDUSE_BUFFER = 400, 100   # metres; housearea/PLAN.md


def house_step(B, landuse, coast, city_buffer=HOUSE_CITY_BUFFER, landuse_buffer=HOUSE_LANDUSE_BUFFER):
    """Building-scale (HOUSE001) only where OSM says someone lives: a place node (city,
    town, village, hamlet) buffered city_buffer metres, or a residential/commercial/
    industrial/retail landuse polygon buffered landuse_buffer metres (housearea/PLAN.md)."""
    assert landuse, '--house needs --landuse for its box'
    from house_encode import file_name
    t = OSM
    pj, cellsj = osm_paths(B)['populated'], osm_paths(B)['cells']
    cells = {tuple(c) for c in json.load(open(cellsj))}
    names = [file_name(*c) for c in cells]
    bj, rj = B + '/house/landuse.json', B + '/house/roads.json'
    return [dict(name='house', cwd=OSM_ROOT,
                 cmds=[['uv', 'run', '--with', 'osmium', '--with', 'shapely', 'python', t + '/extract_landuse.py',
                        PBF, bj, '--bbox', landuse, '--buildings'],
                       ['uv', 'run', '--with', 'osmium', '--with', 'shapely', 'python', t + '/osm_roads.py',
                        PBF, rj, '--box', landuse],
                       ['uv', 'run', '--with', 'shapely', 'python', t + '/house_encode.py', bj,
                        B + '/house', '--template', B + '/tiles/B60R080R.DAT', '--bbox', landuse, '--roads', rj,
                        '--coast', coast, '--dense', str(HOUSE_DENSE), '--stats', B + '/house/stats.json',
                        '--cells', cellsj]],
                 inputs=[PBF, coast, pj, cellsj], tools=[t + '/extract_landuse.py', t + '/osm_roads.py', t + '/house_encode.py',
                                      t + '/bg_encode.py'],
                 products=[bj, rj, B + '/house/stats.json'],
                 outputs={'HOUSE001/' + n: ('file', B + '/house/' + n) for n in names})]


def osm_paths(B):
    o = os.path.join(B, 'osm')
    return dict(turns=o + '/turns.json', coast=o + '/coast.json', populated=o + '/populated.json',
                cells=o + '/house_cells.json')


def replace_cmd(tmp, dest):
    """A step command that moves a finished temp file to its name, so a killed run leaves none."""
    return [sys.executable, '-c', 'import os, sys; os.replace(sys.argv[1], sys.argv[2])', tmp, dest]


def tmp_name(p):
    root, ext = os.path.splitext(p)
    return root + '.tmp' + ext


def osm_steps(B, landuse=None, house=False, city_buffer=HOUSE_CITY_BUFFER, landuse_buffer=HOUSE_LANDUSE_BUFFER):
    """What the later steps read off the OSM extract besides their own reads: turn
    restrictions, the coastline and, for --house, the populated cells. Each is a stamped step
    with the extract as input, so a new extract or box remakes it."""
    t = OSM
    o = osm_paths(B)
    osm_py = ['uv', 'run', '--with', 'osmium', '--with', 'shapely', 'python']
    out = [dict(name='turns', cwd=OSM_ROOT, cmds=[osm_py + [t + '/osm_turns.py', PBF, tmp_name(o['turns'])],
                                                  replace_cmd(tmp_name(o['turns']), o['turns'])],
                inputs=[PBF], products=[o['turns']], outputs={}),
           dict(name='coast', cwd=OSM_ROOT, cmds=[osm_py + [t + '/extract_coast.py', PBF, tmp_name(o['coast'])],
                                                  replace_cmd(tmp_name(o['coast']), o['coast'])],
                inputs=[PBF], products=[o['coast']], outputs={})]
    if house:
        assert landuse, '--house needs --landuse for its box'
        from house_encode import box_cells
        all_cells = [list(c) for c in box_cells(*map(float, landuse.split(',')))]
        pj, cj = o['populated'], o['cells']
        code = ('import json, sys; sys.path.insert(0, %r); from house_encode import populated_cells; '
                'cells = [tuple(c) for c in %r]; '
                'json.dump([list(c) for c in sorted(populated_cells(%r, cells, %r, %r))], open(%r, "w"))'
                % (t, all_cells, pj, city_buffer, landuse_buffer, tmp_name(cj)))
        out.append(dict(name='populated', cwd=OSM_ROOT,
                        cmds=[osm_py + [t + '/extract_landuse.py', PBF, tmp_name(pj), '--bbox', landuse, '--populated',
                                        '--kinds', 'populated_landuse,place'],
                              replace_cmd(tmp_name(pj), pj),
                              ['uv', 'run', '--with', 'shapely', 'python', '-c', code],
                              replace_cmd(tmp_name(cj), cj)],
                        inputs=[PBF], tools=[t + '/house_encode.py'], products=[pj, cj], outputs={}))
    return out


def icode_step(B, icode_base):
    """The two ICODE files, from the unit's own ICODE input and the address register."""
    addr = os.path.join(B, 'addr')
    i1, i2 = os.path.join(B, 'in', 'REFER001_ICODE001.DAT'), os.path.join(B, 'in', 'REFER002_ICODE001.DAT')
    return dict(name='icode', cwd=REPO,
                cmd=[sys.executable, HERE + '/addr/build_icode_clean.py', i1, i2, addr],
                # the town rows come from the address register
                inputs=[i1, i2, ADDRESSES, POSTCODES], extract=[(icode_base, 'REFER001/ICODE001.DAT', i1),
                                                               (icode_base, 'REFER002/ICODE001.DAT', i2)],
                tools=[HERE + '/addr/build_icode_clean.py', HERE + '/addr/addr_csv.py'],
                config=['addr'],
                note='reads the input ICODE files for their volume table (languages and frame kinds); '
                     'REFER001 also copies their 18 POI genre frames byte for byte',
                outputs={'REFER001/ICODE001.DAT': ('file', addr + '/REFER001_ICODE001.DAT'),
                         'REFER002/ICODE001.DAT': ('file', addr + '/REFER002_ICODE001.DAT')})


def steps(B, coast, icode_base, roads=False, also=None, to=None, by=None, area=None, landuse=None, kinds=None,
          buildings=None, house=False, coarse_graph=False, city_buffer=HOUSE_CITY_BUFFER,
          landuse_buffer=HOUSE_LANDUSE_BUFFER):
    uv = ['uv', 'run', '--with', 'shapely', 'python']
    t = OSM
    lu = B + '/landuse/landuse.json'
    lu_step = [dict(name='landuse', cwd=OSM_ROOT,
                    cmd=['uv', 'run', '--with', 'osmium', '--with', 'shapely', 'python', t + '/extract_landuse.py',
                         PBF, lu, '--bbox', landuse] + (['--kinds', kinds + (',building' if buildings else '')]
                                                        if kinds else []) + (['--buildings'] if buildings else []),
                    inputs=[PBF], tools=[t + '/extract_landuse.py'], products=[lu], outputs={})] if landuse else []
    addr = os.path.join(B, 'addr')
    tiles_out = {'MAPAL001/%s.DAT' % c: ('file', B + '/tiles/%s.DAT' % c) for c in CELLS}
    shapes = [B + '/rd/%s.shapes.json' % c for c in CELLS] if roads else []
    l2_tiles = [B + '/rd/%s.DAT' % c for c in CELLS] if area and coarse_graph else []
    out = lu_step + [
        dict(name='tiles', cwd=OSM_ROOT,
             cmd=uv + [t + '/bg_tiles.py', '--profile', PROFILE, '--coastline', coast,
                       '--data-version', VERSION] + (['--landuse', lu] if landuse else [])
                 + (['--area-class', 'building=' + buildings] if buildings else []) + [B + '/tiles'],
             inputs=[coast, PROFILE] + ([lu] if landuse else []),
             tools=[t + '/bg_tiles.py', t + '/bg_encode.py', t + '/clarion_bg.py', t + '/tile_header.py',
                    t + '/clarion_tile.py'],
             products=[p for _, p in tiles_out.values()] if roads else [],
             outputs={} if roads else tiles_out),
    ] + (road_steps(B, also, to, by, area) if roads else []) + \
        (house_step(B, landuse, coast, city_buffer, landuse_buffer) if house else []) + [
        dict(name='lev', cwd=OSM_ROOT,
             cmd=uv + [t + '/lev_build.py', '--profile', PROFILE, '--coastline', coast]
                 + (['--roads', B + '/roads/roads.json'] + [x for p in l2_tiles for x in ('--l2-tile', p)] if area else [])
                 + ['--data-version', VERSION, B + '/LEV3456R.DAT'],
             inputs=[coast, PROFILE] + ([B + '/roads/roads.json'] if area else []) + l2_tiles,
             tools=[t + '/lev_build.py', t + '/lev_file.py', t + '/bg_tiles.py', t + '/bg_encode.py',
                    t + '/clarion_bg.py', t + '/tile_header.py'] + ([t + '/lev_roads.py', t + '/lev_coarse.py', t + '/fa_encode.py',
                                                                      t + '/rd_build.py', t + '/rd_encode.py', t + '/clarion_rd.py',
                                                                      t + '/lev_graph.py', t + '/tile_scan.py'] if area else []),
             # with --area this reads the rd step's tiles and writes L2 nflags into them in
             # place, so a correct rerun must re-stamp rd's outputs too, not just its own
             mutates=(['rd'] if area and coarse_graph else []),
             outputs={'MAPAL001/LEV3456R.DAT': ('file', B + '/LEV3456R.DAT')}),
        icode_step(B, icode_base),
        dict(name='addr', cwd=REPO,
             cmd=[sys.executable, HERE + '/addr/build_addr_area0.py', addr] + (['--shapes'] + shapes if shapes else [])
                 + (['--area', area] if area else []) + (['--only', ADDR_ONLY] if area and ADDR_ONLY else [])
                 + ['--addresses', ADDRESSES, '--postcodes', POSTCODES],
             inputs=[ADDRESSES, POSTCODES] + shapes, config=['addr'],
             tools=[HERE + '/addr/build_addr_area0.py', HERE + '/addr/addr_csv.py', t + '/addre_io.py']
                   + [HERE + '/addr/build_icode_clean.py'] + [
                    t + '/zcode_io.py', t + '/point_io.py', t + '/frames.py', t + '/refer_header.py',
                    t + '/shape_index.py'],
             outputs=dict({'REFER001/%s.cmp' % n: ('cmp', addr + '/' + (n if n != 'COORD001.DAT' else 'REFER001_COORD001.DAT'))
                           for n in PACKED},
                          **{'REFER001/%s.ind' % n: ('ind', addr + '/' + (n if n != 'COORD001.DAT' else 'REFER001_COORD001.DAT'))
                             for n in PACKED},
                          **{'REFER002/COORD001.DAT': ('file', addr + '/REFER002_COORD001.DAT')}),
             note='.cmp/.ind made by build_clean2.wrap (zlib level 6 per 64 KiB block)'),
    ]
    return out


def tool_groups(tools):
    """Split a step's tool files by which git worktree they live under, so a stamp's
    diff check stays scoped to the files that step actually runs, not the whole repo."""
    groups = []
    for repo in (OSM_ROOT, REPO):
        files = [p for p in tools if p.startswith(repo + os.sep)]
        if files:
            groups.append((repo, files))
    return groups


def prep(s):
    """Fill in the fields buildstamp needs from this project's own step shape. The tools are
    every file the step's scripts import; the args are its commands, the card files it reads,
    the config tables its tools read through $O2C_CONFIG and the DATA VERSION."""
    cmds = s.get('cmds') or [s.get('cmd')]
    scripts = [a for c in cmds for a in c if a.endswith('.py') and os.path.isfile(a)]
    code = [c[c.index('-c') + 1] for c in cmds if '-c' in c]
    s['tools'] = sorted(set(s.get('tools', [])) |
                        set(buildstamp.import_closure(scripts, (HERE, os.path.join(HERE, 'addr')), code)))
    s['args'] = dict(cmds=cmds, extract=[[img, path] for img, path, _ in s.get('extract', [])],
                     config={k: CONFIG.get(k) for k in s.get('config', ())}, version=VERSION)
    s['output_files'] = sorted({p for _, p in s['outputs'].values()} | set(s.get('products', [])))
    s['tool_groups'] = tool_groups(s.get('tools', []))
    return s


def run_steps(S, B, reuse, force=False):
    """Run or reuse each step; True when any ran. force reruns them all."""
    os.makedirs(os.path.join(B, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(B, 'in'), exist_ok=True)
    for s in S:
        prep(s)
        # before the stamps are compared, so a step reads this card's files, not the last one's
        for img, path, dest in s.get('extract', []):
            extract(img, path, dest)

    def run_cmds(s):
        for p in s['output_files']:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(os.path.join(B, 'logs', s['name'] + '.log'), 'w') as log:
            for cmd in s.get('cmds', [s.get('cmd')]):
                subprocess.run(cmd, cwd=s['cwd'], stdout=log, stderr=subprocess.STDOUT, check=True)

    buildstamp.run_steps(B, S, reuse, run_cmds, force=force)
    return any(s['ran'] != 'reused' for s in S)


def expected(S):
    """{card path: (bytes, step, size the index lists)} for every file a step produces."""
    out = {}
    for s in S:
        for path, (kind, src) in s['outputs'].items():
            data = open(src, 'rb').read()
            n = len(data)
            if kind != 'file':
                data = wrap(data)[0 if kind == 'cmp' else 1]
            out[path] = (data, s, n)
    return out


def command(s):
    return ' && '.join(' '.join(c) for c in s.get('cmds', [s.get('cmd')]))

GROUPS = dict(lev=lambda p: p == 'MAPAL001/LEV3456R.DAT',
              tiles=lambda p: p.startswith('MAPAL001/B'),
              addr=lambda p: p.startswith(('REFER001/', 'REFER002/')),
              house=lambda p: p.startswith('HOUSE001/'))


def purge(ed, drs, d, keep):
    """Delete the files in folder d that `keep` (a set of card paths) lacks, and their index rows.
    The dot entries stay. Returns the index."""
    gone = [g.name for g in ed.load(d)[1] if '%s/%s' % (d, g.name) not in keep
            and not g.name.startswith('.')]
    for n in gone:
        ed.delete(d, n)
        print('deleted %s/%s' % (d, n))
    rec = [r for r in dir_records(drs) if r['name'] == d][0]
    listed = {n for n, _, _ in children(drs, rec)}
    # the index lists a packed file once, by its plain name
    rows = {n[:-4] if n.endswith('.cmp') else n for n in gone if not n.endswith('.ind')} & listed
    return drstr_remove(drs, d, sorted(rows)) if rows else drs


def assemble(img, exp, groups):
    """Write the chosen generated files (old clusters zeroed), then the index sizes, CMP.LST
    and MMAIN's level counts."""
    ed = Editor(img)
    drs = ed_read(ed, '', 'DRSTR001.DAT')
    sizes = {}
    if 'house' in groups:   # the building-scale folder is all this build's; leftovers of an older card go
        drs = purge(ed, drs, 'HOUSE001', set(exp))
    for path in sorted(exp):
        if not any(GROUPS[g](path) for g in groups):
            continue
        data, _, n = exp[path]
        d, name = path.split('/')
        if ed.find(d, name) is not None and ed_read(ed, d, name) == data:
            print('same   %s %d B' % (path, len(data)))
            continue
        ed.write(d, name, data)
        print('wrote  %s %d B' % (path, len(data)))
        if not name.endswith('.ind'):
            sizes.setdefault(d, {})[name[:-4] if name.endswith('.cmp') else name] = n
    for d, sz in sizes.items():
        rec = [r for r in dir_records(drs) if r['name'] == d][0]
        listed = {n for n, _, _ in children(drs, rec)}
        new = [(n, s) for n, s in sorted(sz.items()) if n not in listed]
        if new:
            drs = drstr_add(drs, d, new)[0]
        drs = set_sizes(drs, d, {n: s for n, s in sz.items() if n in listed})
    lst = cmp_lst_ed(ed)
    if ed_read(ed, '', 'CMP.LST') != lst:
        ed.write('', 'CMP.LST', lst)
        drs = set_sizes(drs, '/', {'CMP.LST': len(lst)})
        print('CMP.LST rewritten')
    ed.write('', 'DRSTR001.DAT', drs)
    heads = [ed_read(ed, 'MAPAL001', g.name)[:0x400]
             for g in ed.load('MAPAL001')[1] if g.name.endswith('.DAT')]
    house = [g for g in ed.load('HOUSE001')[1] if g.name.endswith('.DAT')]
    ed.write('', 'MMAIN001.DAT', mmain_card(ed_read(ed, '', 'MMAIN001.DAT'), heads, len(house)))
    free = ed.close()
    print('clusters zeroed %d, free after %d' % (ed.zeroed, free))


def cmp_lst_ed(ed):
    """cmp_lst, read through the open editor."""
    names = ['\\SystemSD\\%s\\%s' % (d, g.name[:-4]) for d in SCOPE
             for g in ed.load(d)[1] if g.name.endswith('.cmp')]
    return '\0'.join(sorted(names)).encode('ascii')


def copy_map(img, src):
    """Write the map, address and POI files of the finished card `src` onto the fresh card `img`
    as they are, ICODE included since ADDRE's town indices point into it: nothing is regenerated
    or restamped, and the target's app and settings stay its own. Files the target has in those
    folders and `src` lacks are deleted, and the index, CMP.LST and MMAIN are made for the target
    the way assemble makes them. Returns the paths copied."""
    files = {p: b for p, b in card_files(src).items() if p.split('/')[0] in SCOPE}
    exp = {}
    for p, b in files.items():
        n = struct.unpack_from('<I', files[p[:-4] + '.ind'])[0] if p.endswith('.cmp') else len(b)
        exp[p] = (b, None, n)
    ed = Editor(img)
    drs = ed_read(ed, '', 'DRSTR001.DAT')
    for d in SCOPE:
        drs = purge(ed, drs, d, set(files))
    ed.write('', 'DRSTR001.DAT', drs)
    ed.close()
    assemble(img, exp, list(GROUPS))      # MMAIN from the copied headers, as a full build makes it
    return sorted(files)


def manifest(img, S, B, exp):
    files = card_files(img)
    recipe = os.path.abspath(__file__)
    tools_rev = dict(osm2clarion=git_rev(OSM_ROOT), leaf_is_map=git_rev(REPO))
    diff_dest = os.path.splitext(final_name(img))[0] + '.o2c.diff'
    o2c_dirty, o2c_diff_sha = buildstamp.save_diff_if_dirty(OSM_ROOT, diff_dest)
    osm2clarion = dict(head=buildstamp.git_head(OSM_ROOT), dirty=o2c_dirty, diff_sha256=o2c_diff_sha,
                        diff_file=os.path.basename(diff_dest) if o2c_dirty else None)
    leaf = buildstamp.repo_manifest_state(REPO)
    index_bad = drstr_check(files['DRSTR001.DAT'], files)
    rows = []
    for path in sorted(files):
        data = files[path]
        row = dict(path=path, size=len(data), sha256=sha(data))
        if path in exp and exp[path][0] == data:
            s = exp[path][1]
            row.update(status='generated', step=s['name'], command=command(s),
                       inputs=[dict(path=p, sha256=sha_file(p)) for p in s['inputs']],
                       tools=[dict(path=p, sha256=sha_file(p)) for p in s['tools']])
            if s.get('extract'):
                row['inputs_from'] = ['%s off %s' % (pp, i) for i, pp, _ in s['extract']]
            if s.get('note'):
                row['note'] = s['note']
        elif path == 'CMP.LST' and data == cmp_lst(img):
            row.update(status='generated', step='assemble', command=recipe + ' (cmp_lst)',
                       note="the card's .cmp files, sorted")
        elif path == 'DRSTR001.DAT':
            bad = index_bad
            row.update(status='edited', step='assemble', command=recipe + ' (set_sizes)',
                       note="the base card's index of every folder; sizes rewritten by this build and "
                            "build_gen_card/build_clean2 before it. Scoped rows checked against the card: "
                            + ('all match' if not bad else '; '.join(bad)))
        elif path == 'MMAIN001.DAT':
            row.update(status='edited', step='assemble', command=recipe + ' (build_clean2.mmain)',
                       note="the base card's master header; level counts summed from every MAPAL001 "
                            "header, db box copied from those headers, NAVTEQ string kept")
        else:
            row.update(status='leftover',
                       note='differs from its step output' if path in exp else 'no step produces this file')
        rows.append(row)
    missing = sorted(set(exp) - set(files))
    steps_out = [dict(name=s['name'], command=command(s), ran=s.get('ran'),
                       stamp=buildstamp.load_stamp(buildstamp.stamp_path(B, s['name']))) for s in S]
    return dict(card=final_name(img), built=time.strftime('%Y-%m-%d %H:%M:%S'), tools=tools_rev,
                osm2clarion=osm2clarion, leaf=leaf,
                steps=steps_out, files=rows, missing_on_card=missing, index_problems=index_bad)


def report(m):
    by = {}
    for r in m['files']:
        by.setdefault(r['status'], []).append(r)
    for st in ('generated', 'edited'):
        for r in by.get(st, []):
            print('%-9s %-28s %10d  %s  %s' % (st, r['path'], r['size'], r['sha256'][:12], r.get('step', '')))
    left = by.get('leftover', [])
    print()
    if left:
        print('!' * 72)
        print('!!! %d LEFTOVER FILE%s: no step produced %s' % (len(left), 'S' if len(left) > 1 else '',
                                                          'them' if len(left) > 1 else 'it'))
        for r in left:
            print('!!!   %-28s %10d  %s  (%s)' % (r['path'], r['size'], r['sha256'][:12], r['note']))
        print('!' * 72)
    else:
        print('leftovers: none')
    if m['missing_on_card']:
        print('!!! produced but not on the card:', ', '.join(m['missing_on_card']))


def card_problems(m):
    """Leftovers the config does not allow, produced files missing from the card, and index
    rows that disagree with it."""
    allow = CONFIG.get('allow_leftovers', [])
    out = ['leftover %s (%s)' % (r['path'], r['note']) for r in m['files']
           if r['status'] == 'leftover' and not any(fnmatch.fnmatch(r['path'], a) for a in allow)]
    out += ['produced but not on the card: ' + p for p in m['missing_on_card']]
    out += ['index: ' + b for b in m.get('index_problems', [])]
    return out


def builds():
    """{(number, unit): slug} of the cards in CARDS/builds."""
    out = {}
    for f in os.listdir(CARDS + '/builds'):
        m = NAME.match(f)
        if m: out[(int(m.group(1)), m.group(2))] = m.group(3)
    return out


def next_number(unit, slug):
    """One past the highest build number, or that number when the newest build is this slug
    on another unit and this unit has no card with it yet."""
    have = builds()
    top = max((n for n, _ in have), default=0)
    twins = [u for (n, u), s in have.items() if n == top and s == slug and u != unit]
    return top if twins and (top, unit) not in have else top + 1


PARTIAL = False     # --partial: write IMG.partial and its manifest, leave the rename and the log to build.py


def new_card(unit, slug, base=None, number=None):
    """(number, path written, base): a fresh cp -c of the base at the build's name, or at that
    name plus .partial with --partial."""
    default_base = CONFIG['card_naming']['base_pattern'].format(prefix=CARD_PREFIX, unit=unit)
    base = os.path.abspath(os.path.expanduser(base or '%s/bases/%s' % (CARDS, default_base)))
    n = number or next_number(unit, slug)
    name = CARD_BUILD_PATTERN.format(prefix=CARD_PREFIX, number=n, unit=unit, slug=slug)
    final = '%s/builds/%s' % (CARDS, name)
    img = final + '.partial' if PARTIAL else final
    for p in (final, img):
        if os.path.exists(p):
            raise SystemExit('%s already exists' % p)
    subprocess.run(['cp', '-c', base, img], check=True)
    os.chmod(img, 0o644)       # the base is read-only and cp keeps its mode
    print('card %s from %s' % (img, base))
    return n, img, base


def final_name(img):
    return img[:-len('.partial')] if img.endswith('.partial') else img


def manifest_name(img):
    """The manifest beside the card, .partial while the card is."""
    m = os.path.splitext(final_name(img))[0] + '.manifest.json'
    return m + '.partial' if img.endswith('.partial') else m


def cards_line(n, unit, slug, what, base):
    if not PARTIAL:
        cards_log.add(CARDS + '/' + CARDS_LOG, n, unit, slug, what, base, CARDS)


def data_version(files):
    import tile_header
    return tile_header.fields(files['MAPAL001/LEV3456R.DAT'][:0x1000])[0]['data_version']


def same_version(files, version, src):
    """Refuse a source card stamped for another unit: --map-from copies stamps as they are."""
    have = data_version(files)
    if have != version:
        raise SystemExit('%s carries DATA VERSION %s, this unit needs %s; use --restamp-from'
                         % (src, have, version))


def map_from(unit, slug, src, base, number, what, B):
    """Build once, write per unit: a card of `unit` carrying the map files of the finished card `src`."""
    src = os.path.abspath(os.path.expanduser(src))
    same_version(card_files(src), VERSION, src)
    n, img, base = new_card(unit, slug, base, number)
    paths = copy_map(img, src)
    got, want = card_files(img), card_files(src)
    bad = [p for p in paths if got.get(p) != want[p]]
    if bad:
        raise SystemExit('copied files differ from %s: %s' % (src, ', '.join(bad)))
    bad = drstr_check(got['DRSTR001.DAT'], got)
    if bad:
        raise SystemExit('index rows off: ' + '; '.join(bad))
    m = dict(card=final_name(img), built=time.strftime('%Y-%m-%d %H:%M:%S'), unit=unit, base=base, number=n, slug=slug,
             map_from=src, tools=dict(osm2clarion=git_rev(OSM_ROOT)),
             files=[dict(path=p, size=len(want[p]), sha256=sha(want[p]), status='copied') for p in paths])
    with open(manifest_name(img), 'w') as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    cards_line(n, unit, slug, what, base)
    print('copied %d files from %s' % (len(paths), src))


def restamp_from(unit, slug, src, base, number, what, B):
    """Build once, stamp per unit: the finished card `src`'s map, address and POI files on this
    unit's base with this unit's DATA VERSION, and the ICODE files built from this unit's input."""
    src = os.path.abspath(os.path.expanduser(src))
    t0 = time.time()
    files = card_files(src)
    old = data_version(files)
    exp, bare = restamped({p: b for p, b in files.items() if p.split('/')[0] in SCOPE}, old, VERSION)
    S = [icode_step(B, UNITS[unit]['icode'])]
    run_steps(S, B, False)
    exp.update(expected(S))
    n, img, base = new_card(unit, slug, base, number)
    ed = Editor(img)
    drs = ed_read(ed, '', 'DRSTR001.DAT')
    for d in SCOPE:
        drs = purge(ed, drs, d, set(exp))
    ed.write('', 'DRSTR001.DAT', drs)
    ed.close()
    assemble(img, exp, list(GROUPS))
    got = card_files(img)
    bad = [p for p in exp if got.get(p) != exp[p][0]]
    if bad:
        raise SystemExit('written files differ from what was meant: ' + ', '.join(bad))
    bad = drstr_check(got['DRSTR001.DAT'], got)
    if bad:
        raise SystemExit('index rows off: ' + '; '.join(bad))
    m = dict(card=final_name(img), built=time.strftime('%Y-%m-%d %H:%M:%S'), unit=unit, base=base, number=n, slug=slug,
             restamp_from=src, data_version=dict(source=old, card=VERSION),
             tools=dict(osm2clarion=git_rev(OSM_ROOT)),
             steps=[dict(name=s['name'], command=command(s), ran=s.get('ran')) for s in S],
             files=[dict(path=p, size=len(exp[p][0]), sha256=sha(exp[p][0]),
                         status='generated' if exp[p][1] else 'restamped' if p not in bare else 'copied')
                    for p in sorted(exp)])
    with open(manifest_name(img), 'w') as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    cards_line(n, unit, slug, what, base)
    print('%d files from %s, DATA VERSION %s -> %s, %d without a stamp: %s'
          % (len(exp), src, old, VERSION, len(bare), ', '.join(bare) or 'none'))
    print('re-stamp took %.0f s' % (time.time() - t0))


def main(argv):
    if '--help' in argv or '-h' in argv:
        print(__doc__)
        return

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    unit, slug = opt('--unit'), opt('--slug')
    if unit not in UNITS or not slug:
        raise SystemExit(__doc__)
    global PARTIAL
    PARTIAL = '--partial' in argv
    B = os.path.abspath([a for i, a in enumerate(argv)
                         if not a.startswith('--') and (i == 0 or not argv[i - 1].startswith('--'))][0])
    global VERSION
    VERSION = UNITS[unit]['version']
    os.environ['CLARION_DATA_VERSION'] = VERSION      # read by tools/addr/iceland_card.py
    if opt('--map-from'):
        return map_from(unit, slug, opt('--map-from'), opt('--base'), int(opt('--number', 0)),
                        opt('--what', slug), B)
    if opt('--restamp-from'):
        return restamp_from(unit, slug, opt('--restamp-from'), opt('--base'), int(opt('--number', 0)),
                            opt('--what', slug), B)
    icode_base = UNITS[unit]['icode']
    coast = opt('--coast')
    also, to, by = opt('--also'), opt('--to'), opt('--by')
    groups = opt('--write', 'lev').split(',')
    assert set(groups) <= set(GROUPS), groups
    area = opt('--area')
    global ADDR_ONLY
    ADDR_ONLY = opt('--addr-only')
    if area:
        area_values(area)
    landuse = opt('--landuse')
    if landuse in AREAS:
        landuse = box_text(AREAS[landuse]['bbox'])
    elif landuse == 'area':
        assert area, '--landuse area needs --area'
        landuse = box_text(area_values(area)[2])
    city_buffer = float(opt('--house-city-buffer', HOUSE_CITY_BUFFER))
    landuse_buffer = float(opt('--house-landuse-buffer', HOUSE_LANDUSE_BUFFER))
    reuse = '--reuse' in argv
    pre = osm_steps(B, landuse, '--house' in argv, city_buffer, landuse_buffer)
    ran = run_steps(pre, B, reuse)
    S = steps(B, coast or osm_paths(B)['coast'], icode_base, roads='--roads' in argv or bool(area), also=also,
              to=to, by=by, area=area, landuse=landuse, kinds=opt('--landuse-kinds'), buildings=opt('--buildings'),
              house='--house' in argv, coarse_graph='--coarse-graph' in argv,
              city_buffer=city_buffer, landuse_buffer=landuse_buffer)
    if '--house' in argv:
        groups.append('house')
    run_steps(S, B, reuse, force=ran)
    S = pre + S
    exp = expected(S)
    n, img, base = new_card(unit, slug, opt('--base'), int(opt('--number', 0)))
    assemble(img, exp, groups)
    m = manifest(img, S, B, exp)
    m.update(unit=unit, base=base, number=n, slug=slug)
    for p in (os.path.join(B, 'manifest.json'), manifest_name(img)):
        with open(p, 'w') as f:
            json.dump(m, f, indent=1, ensure_ascii=False)
    report(m)
    print('manifest:', manifest_name(img))
    problems = card_problems(m)
    if problems:
        raise SystemExit('the card is not what the steps made:\n  ' + '\n  '.join(problems))
    cards_line(n, unit, slug, opt('--what', slug), base)


if __name__ == '__main__':
    main(sys.argv[1:])
