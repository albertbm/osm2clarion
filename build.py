#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the whole chain for one profile.

    ./build.py --profile iceland --base CARD --out DIR [--target v2014] [--from STEP]
    ./build.py --profile finland --base CARD.img --out DIR --osm PBF [--area NAME] [--card OUT.img]

CARD is a mounted base card, or a copy of one: it needs MAPAL001/LEV3456R.DAT,
MAPAL001/<any tile>.DAT as the header template, and MMAIN001.DAT. The card is only read.
DIR gets the files you then copy onto a card: MAPAL001/*.DAT, MMAIN001.DAT and, if the
card is mounted, DRSTR001.DAT.

A profile with `[build] mode = "area"` keeps the base card's own map and rebuilds the roads of one
named area from OSM. --base is then the card image itself, --osm the extract, and --card, if
given, writes the result into a copy of the image. DIR also gets the whole stripped
MAPAL001. The steps for that mode are in `--list`.

Each step is one tool, printed as it runs, so a step can be repeated by hand. --from skips
ahead to a named step and --only runs one; `--list` prints the names.
"""
import os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, 'tools')
sys.path.insert(0, TOOLS)
import clarion_profile


STEPS = ['extract', 'coarse', 'background', 'roads', 'master', 'index']
AREA_STEPS = ['factory', 'osm', 'strip', 'roads', 'coarse', 'sec4', 'master', 'card']


def run(*args):
    cmd = [sys.executable] + [str(a) for a in args]
    shown = ['python3'] + [os.path.relpath(c, HERE) if c.startswith(HERE) else c for c in cmd[1:]]
    print('\n$', ' '.join(shown), flush=True)
    subprocess.run(cmd, check=True)


def tool(name):
    return os.path.join(TOOLS, name)


def steps(P, prof, base, out, target):
    """(name, callable) for each step, in order."""
    mapal = os.path.join(base, 'MAPAL001')
    lev_in = os.path.join(mapal, 'LEV3456R.DAT')
    tiles = sorted(f for f in os.listdir(mapal) if f.startswith('B') and f.endswith('.DAT'))
    template = os.path.join(mapal, tiles[0]) if tiles else None
    outmap = os.path.join(out, 'MAPAL001')
    scratch = os.path.join(out, 'work')
    cells = P.cells
    retarget = (P.targets.get(target) or {}).get('retarget') if target else None

    def extract():
        if not P.osm:
            raise SystemExit('source.osm is not set in the profile')
        run(tool('extract_coast.py'), P.osm, P.coastline)
        run(tool('osm_roads.py'), P.osm, P.roads)

    def coarse():
        a, b, c, d, e = (os.path.join(scratch, n) for n in ('l3.DAT', 'l45.DAT', 'l6.DAT', 'roads.DAT', 'l5roads.DAT'))
        run(tool('lev_append.py'), '--profile', prof, lev_in, a)
        run(tool('lev_coarse.py'), '--profile', prof, a, b)
        run(tool('lev_l6.py'), '--profile', prof, b, c)
        run(tool('lev_roads.py'), '--profile', prof, c, d)
        run(tool('lev5_roads.py'), '--profile', prof, d, e, '--simplify', '500')
        run(tool('lev_compact.py'), e, os.path.join(outmap, 'LEV3456R.DAT'))

    def background():
        run(tool('bg_tiles.py'), '--profile', prof, template, os.path.join(scratch, 'base'), *cells)

    def roads():
        for c in cells:
            src = os.path.join(scratch, 'base', c + '.DAT')
            dest = os.path.join(outmap, c + '.DAT')
            run(tool('rd_build.py'), '--profile', prof, src, dest, c)
            if retarget:
                run(tool('tile_retarget.py'), dest, dest + '.tmp', '--to', retarget)
                os.replace(dest + '.tmp', dest)

    def master():
        run(tool('make_mmain.py'), '--profile', prof,
            os.path.join(base, 'MMAIN001.DAT'), os.path.join(out, 'MMAIN001.DAT'),
            *[os.path.join(outmap, c + '.DAT') for c in cells],
            '--lev', os.path.join(outmap, 'LEV3456R.DAT'))

    def index():
        # Needs the card itself: the index carries every file's size, and the order comes
        # from the card's own copy.
        run(tool('make_drstr_card.py'), base, outmap, os.path.join(out, 'DRSTR001.DAT'))

    return [('extract', extract), ('coarse', coarse), ('background', background),
            ('roads', roads), ('master', master), ('index', index)]

def card_files(base, dest):
    """Copy MAPAL001/*.DAT and MMAIN001.DAT off a card image, or a mounted card, into dest."""
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(base):
        mapal = os.path.join(base, 'MAPAL001')
        for n in os.listdir(mapal):
            if n.endswith('.DAT'): shutil.copyfile(os.path.join(mapal, n), os.path.join(dest, n))
        shutil.copyfile(os.path.join(base, 'MMAIN001.DAT'), os.path.join(dest, 'MMAIN001.DAT'))
        return
    sys.path.insert(0, TOOLS)
    import fatread
    with fatread.Fat32(base) as fs:
        for path, size, cluster, _ in fs.walk():
            if size and (path == '/MMAIN001.DAT' or (path.startswith('/MAPAL001/') and path.endswith('.DAT'))):
                with open(os.path.join(dest, path.split('/')[-1]), 'wb') as f:
                    f.write(fs.f.read_clusters(cluster, size))


def area_steps(P, prof, base, out, area, osm, card):
    """The steps for a profile in area mode: factory tiles stay, one area's roads are rebuilt."""
    areas = P.areas
    if area is None and len(areas) == 1: area = next(iter(areas))
    if area not in areas:
        raise SystemExit('pass --area NAME, one of: %s' % (', '.join(areas) or 'none, the profile defines no areas'))
    A = areas[area]
    if len(A['cells']) != 1:
        raise SystemExit('area %s lists %d cells; area mode rebuilds exactly one' % (area, len(A['cells'])))
    cell = A['cells'][0]
    B = P.build
    outmap = os.path.join(out, 'MAPAL001')
    work = os.path.join(out, 'work')
    fac = os.path.join(work, 'factory')
    j = lambda *a: os.path.join(work, *a)
    levs = j('LEV3456R.stripped.DAT')
    tile_src, tile_roads = j(cell + '.stripped.DAT'), j(cell + '.roads.DAT')
    roads, turns_all, turns = j('roads.json'), j('turns_all.json'), j('turns.json')
    lev_out = os.path.join(outmap, 'LEV3456R.DAT')

    def need_osm():
        path = osm or P.osm
        if not path or not os.path.exists(path):
            raise SystemExit('no OSM extract: pass --osm FILE.osm.pbf')
        return path

    def factory():
        card_files(base, fac)

    def osm_step():
        pbf = need_osm()
        run(tool('osm_turns.py'), pbf, turns_all)
        run(tool('osm_roads.py'), pbf, roads, '--public', '--relations', ','.join(map(str, A['relations'])),
            '--turns', turns_all, turns, '--ramp-names')

    def strip():
        pairs = []
        for n in sorted(os.listdir(fac)):
            if not n.endswith('.DAT') or n == 'MMAIN001.DAT': continue
            dest = levs if n == 'LEV3456R.DAT' else tile_src if n == cell + '.DAT' else os.path.join(outmap, n)
            pairs += [os.path.join(fac, n), dest]
        run(tool('road_strip.py'), '--profile', prof, *pairs)

    def roads_step():
        args = ['--keep-names'] * bool(B.get('keep_names')) + ['--upper-names'] * bool(B.get('upper_names'))
        if B.get('turn_restrictions'): args += ['--turn-restrictions', turns]
        run(tool('rd_build.py'), '--profile', prof, *args, tile_src, tile_roads, cell, roads)

    def coarse():
        # With the graph on, this also writes hierarchy flags into the roads tile.
        graph = ['--l2-tile', tile_roads] if B.get('coarse_graph') else []
        run(tool('lev_add_roads.py'), '--profile', prof, *graph, levs, lev_out, roads)

    def sec4():
        dest = os.path.join(outmap, cell + '.DAT')
        if B.get('sec4'):
            run(tool('sec4_write.py'), tile_roads, dest, '--country', P.country)
        else:
            shutil.copyfile(tile_roads, dest)

    def tiles():
        names = sorted(n for n in os.listdir(outmap) if n.startswith('B') and n.endswith('.DAT'))
        return [os.path.join(outmap, n) for n in names if n != cell + '.DAT'] + [os.path.join(outmap, cell + '.DAT')]

    def master():
        run(tool('make_mmain.py'), '--profile', prof, os.path.join(fac, 'MMAIN001.DAT'),
            os.path.join(out, 'MMAIN001.DAT'), *tiles(), '--lev', lev_out)

    def card_step():
        if not card:
            print('no --card given; copy %s onto a card yourself' % out)
            return
        if not os.path.isfile(base):
            raise SystemExit('--card needs --base to be a card image')
        # a fixed write time, so two runs stamp the image alike; set SOURCE_DATE_EPOCH to override
        os.environ.setdefault('SOURCE_DATE_EPOCH', '946684800')
        if subprocess.run(['cp', '-c', base, card], stderr=subprocess.DEVNULL).returncode:
            shutil.copyfile(base, card)
        os.chmod(card, 0o644)
        ts = tiles()
        run(tool('fatwrite.py'), card, '--write',
            *['MAPAL001:%s=%s' % (os.path.basename(t), t) for t in ts],
            'MAPAL001:LEV3456R.DAT=' + lev_out, ':MMAIN001.DAT=' + os.path.join(out, 'MMAIN001.DAT'))
        run(tool('drstr_refresh.py'), card, *['MAPAL001:' + os.path.basename(t) for t in ts],
            'MAPAL001:LEV3456R.DAT')

    return [('factory', factory), ('osm', osm_step), ('strip', strip), ('roads', roads_step),
            ('coarse', coarse), ('sec4', sec4), ('master', master), ('card', card_step)]


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    prof = P.path or 'default'
    base = out = None
    target = P.targets.get('default')
    first = only = area = osm = card = None
    for flag, setter in (('--base', 'base'), ('--out', 'out'), ('--target', 'target'),
                         ('--from', 'first'), ('--only', 'only'), ('--area', 'area'),
                         ('--osm', 'osm'), ('--card', 'card')):
        if flag in argv:
            i = argv.index(flag)
            value = argv[i + 1]
            del argv[i:i + 2]
            if setter == 'base': base = value
            elif setter == 'out': out = value
            elif setter == 'target': target = value
            elif setter == 'first': first = value
            elif setter == 'only': only = value
            elif setter == 'area': area = value
            elif setter == 'osm': osm = value
            else: card = value
    area_mode = P.build.get('mode') == 'area'
    if '--list' in argv:
        print('\n'.join(AREA_STEPS if area_mode else STEPS))
        return
    if not base or not out:
        raise SystemExit(__doc__)
    if not P.cells:
        raise SystemExit('profile %s lists no cells' % P.name)
    for d in (out, os.path.join(out, 'MAPAL001'), os.path.join(out, 'work'),
              os.path.join(out, 'work', 'base')):
        if area_mode and d.endswith('base'): continue
        os.makedirs(d, exist_ok=True)
    plan = area_steps(P, prof, base, out, area, osm, card) if area_mode else steps(P, prof, base, out, target)
    names = [n for n, _ in plan]
    if only:
        plan = [(n, f) for n, f in plan if n == only] or sys.exit('no step %r (have: %s)' % (only, ', '.join(names)))
    elif first:
        if first not in names: raise SystemExit('no step %r (have: %s)' % (first, ', '.join(names)))
        plan = plan[names.index(first):]
    elif not area_mode:
        plan = plan[1:]      # the extract step needs osmium; ask for it by name
    for name, fn in plan:
        print('\n===== %s' % name, flush=True)
        fn()
    print('\ndone. %s holds the files to copy onto the card.' % out)


if __name__ == '__main__':
    main()
