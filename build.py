#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a card from a profile.

    ./build.py --profile NAME --base CARD --out OUT.img [--work DIR] [--osm PBF] [--area NAME,...]
               [--local FILE] [--what TEXT] [--prev IMG] [--stage DIR] [--only STEP | --from STEP] [--list]

Everything specific to a region, a unit or a data source is in profiles/NAME.toml. [build] mode
picks the chain:

  generate  A card from scratch on the unit's base card: background, roads, coarse file,
            building-scale map, addresses and POI icons for the profile's areas
            (build_gen_lev.py, poi_apply.py, build_poicap.py). OUT is
            CARDS/builds/<prefix>-<number>-<unit>-<slug>.img; the unit and slug are read from
            that name, and CARDS is the directory two levels up.
  area      The base card keeps its own map. Its roads are stripped and one area's roads are
            rebuilt from OSM. --base is the card image, only read.

--work holds everything generated (default build/<name of OUT>). The profile's `local` file, or
--local, is merged over the profile table by table; it holds paths and values that belong to one
machine. Each step prints the command it runs, so a step can be repeated by hand. --list names
the steps, --only runs one and --from resumes at one.
"""
import glob, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, 'tools')
sys.path.insert(0, TOOLS)
import clarion_profile

GENERATE_STEPS = ['prep', 'cards', 'poi', 'icons', 'addr', 'consent', 'check']
AREA_STEPS = ['factory', 'osm', 'strip', 'roads', 'coarse', 'sec4', 'master', 'card']


def run(*args, env=None):
    cmd = [str(a) for a in args]
    if cmd[0].endswith('.py'): cmd.insert(0, sys.executable)
    shown = ['python3' if c == sys.executable else os.path.relpath(c, HERE) if c.startswith(HERE) else c
             for c in cmd]
    print('\n$', ' '.join(shown), flush=True)
    subprocess.run(cmd, check=True, env=env)


def tool(name):
    return os.path.join(TOOLS, name)


def expand(path):
    return os.path.expanduser(path)


def merge(a, b):
    """b over a, tables merged and everything else replaced."""
    out = dict(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def card_files(base, dest):
    """Copy MAPAL001/*.DAT and MMAIN001.DAT off a card image, or a mounted card, into dest."""
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(base):
        mapal = os.path.join(base, 'MAPAL001')
        for n in os.listdir(mapal):
            if n.endswith('.DAT'): shutil.copyfile(os.path.join(mapal, n), os.path.join(dest, n))
        shutil.copyfile(os.path.join(base, 'MMAIN001.DAT'), os.path.join(dest, 'MMAIN001.DAT'))
        return
    import fatread
    with fatread.Fat32(base) as fs:
        for path, size, cluster, _ in fs.walk():
            if size and (path == '/MMAIN001.DAT' or (path.startswith('/MAPAL001/') and path.endswith('.DAT'))):
                with open(os.path.join(dest, path.split('/')[-1]), 'wb') as f:
                    f.write(fs.f.read_clusters(cluster, size))


def card_tiles(base):
    """Names of the tiles in the card's MAPAL001 folder, the only record of which cells it ships."""
    if os.path.isdir(base):
        names = os.listdir(os.path.join(base, 'MAPAL001'))
    else:
        import fatread
        with fatread.Fat32(base) as fs:
            names = [p.split('/')[-1] for p, size, _, _ in fs.walk() if size and p.startswith('/MAPAL001/')]
    return [n[:-4] for n in names if re.fullmatch(r'B[0-9A-F]{2}R[0-9A-F]{3}R\.DAT', n)]


def profile_with_cells(P, prof, base, work):
    """The profile path to hand the tools: the profile itself, or a copy with the cells the
    base card and the profile's boxes give when it lists none."""
    if P.cells:
        return P, prof
    if not P.boxes:
        raise SystemExit('profile %s lists no cells and no bbox' % P.name)
    cells = clarion_profile.cells_for(P.boxes, card_tiles(base))
    if not cells:
        raise SystemExit('no tile on %s lies in the bbox of profile %s' % (base, P.name))
    print('cells from the card and the bbox:', ' '.join(cells))
    with open(P.path, encoding='utf-8') as f:
        text = f.read()
    text, n = re.subn(r'^\[grid\]\n', '[grid]\ncells = [%s]\n' % ', '.join('"%s"' % c for c in cells),
                      text, count=1, flags=re.M)
    if not n:
        raise SystemExit('profile %s has no [grid] table' % P.name)
    # one level below the work dir, so the profile's relative paths still resolve to its parent
    path = os.path.join(work, 'profiles', os.path.basename(P.path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return clarion_profile.load(path), path


def area_steps(P, prof, base, out, work, area, osm):
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
    outmap = os.path.join(work, 'MAPAL001')
    fac = os.path.join(work, 'factory')
    j = lambda *a: os.path.join(work, *a)
    levs = j('LEV3456R.stripped.DAT')
    tile_src, tile_roads = j(cell + '.stripped.DAT'), j(cell + '.roads.DAT')
    roads, turns_all, turns = j('roads.json'), j('turns_all.json'), j('turns.json')
    lev_out = os.path.join(outmap, 'LEV3456R.DAT')
    mmain = j('MMAIN001.DAT')

    def need_osm():
        path = osm or P.osm
        if not path or not os.path.exists(path):
            raise SystemExit('no OSM extract: pass --osm FILE.osm.pbf')
        return path

    def factory():
        os.makedirs(outmap, exist_ok=True)
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
        run(tool('make_mmain.py'), '--profile', prof, os.path.join(fac, 'MMAIN001.DAT'), mmain, *tiles(),
            '--lev', lev_out)

    def card_step():
        if not os.path.isfile(base):
            raise SystemExit('--base must be a card image')
        # a fixed write time, so two runs stamp the image alike; set SOURCE_DATE_EPOCH to override
        os.environ.setdefault('SOURCE_DATE_EPOCH', '946684800')
        if subprocess.run(['cp', '-c', base, out], stderr=subprocess.DEVNULL).returncode:
            shutil.copyfile(base, out)
        os.chmod(out, 0o644)
        ts = tiles()
        run(tool('fatwrite.py'), out, '--write',
            *['MAPAL001:%s=%s' % (os.path.basename(t), t) for t in ts],
            'MAPAL001:LEV3456R.DAT=' + lev_out, ':MMAIN001.DAT=' + mmain)
        run(tool('drstr_refresh.py'), out, *['MAPAL001:' + os.path.basename(t) for t in ts],
            'MAPAL001:LEV3456R.DAT')

    return [('factory', factory), ('osm', osm_step), ('strip', strip), ('roads', roads_step),
            ('coarse', coarse), ('sec4', sec4), ('master', master), ('card', card_step)]


def make_config(P, local, cells, base, osm, work):
    """The JSON config the generator tools read, from the profile and the local file."""
    raw = merge(P.raw, local)
    B = raw.get('build', {})
    root = os.path.dirname(os.path.dirname(P.path))

    def path(p):
        p = expand(p)
        return p if os.path.isabs(p) or '$' in p else os.path.join(root, p)

    for table, key in (('addr', 'poc_streets'), ('addr', 'poc_anchor'), ('poicap', 'addr_only_default')):
        if key not in raw.get(table, {}):
            raise SystemExit('no [%s] %s: the profile leaves it to the local file' % (table, key))
    data = {k: path(v) for k, v in raw['data'].items()}
    if osm: data['pbf'] = os.path.abspath(osm)
    units = {}
    for u, c in raw['units'].items():
        units[u] = dict(c, icode=c.get('icode') or os.path.abspath(base))
    cfg = dict(
        units=units, cells=cells, sec4_cells=B['sec4_cells'],
        areas={n: dict(relations=a['relations'], cells=a['cells'], bbox=a['bbox']) for n, a in raw['areas'].items()},
        dealer=raw['dealer'], road_tiles=B['road_tiles'], data=data,
        camera_speed_fallback=raw.get('camera_speed_fallback', {}), card_naming=raw['card_naming'],
        cards_log=B['cards_log'], poicap=raw['poicap'], poi_python=B.get('poi_python', 'python3'),
        roads_poc=raw['roads_poc'], addr=raw['addr'])
    return cfg


def generate_steps(P, prof, base, out, work, areas, osm, local, args):
    """The steps for a profile in generate mode: the chain every Iceland card is built by."""
    B = P.build
    cfg = make_config(P, local, P.cells, base, osm, work)
    naming = cfg['card_naming']
    pattern = re.compile(r'%s-(\d{3})-(%s)-(.+)\.img$' % (naming['prefix'], '|'.join(naming['units'])))
    m = pattern.match(os.path.basename(out))
    if not m or os.path.basename(os.path.dirname(out)) != 'builds':
        raise SystemExit('--out must be CARDS/builds/%s-NNN-%s-SLUG.img' % (naming['prefix'], '|'.join(naming['units'])))
    unit, slug = m.group(2), m.group(3)
    cards = os.path.dirname(os.path.dirname(out))
    manifest = out[:-4] + '.manifest.json'
    area_arg = areas or ','.join(cfg['areas'])
    stage = args.get('stage') or os.path.join(cards, 'stage')
    version = cfg['units'][unit]['version']
    conf = os.path.join(work, 'config', P.name + '.json')
    poi_py = expand(cfg['poi_python'])
    pbf, turns, coast = cfg['data']['pbf'], cfg['data']['turns'], cfg['data']['coast']
    env = dict(os.environ, O2C_CONFIG=conf, CARDS_DIR=cards, OSM2CLARION=HERE,
               SOURCE_DATE_EPOCH=os.environ.get('SOURCE_DATE_EPOCH', '946684800'))
    py = sys.executable
    # the address and POI steps stamp the unit's DATA VERSION into what they write
    env_v = dict(env, CLARION_DATA_VERSION=version)
    s1, s2 = (os.path.join(stage, '%s-%s.img' % (k, unit)) for k in ('s1', 's2'))

    def stamp(name, cmd, target):
        run(py, tool('buildstamp.py'), 'post-step', manifest, name, cmd, target, env=env_v)

    def prep():
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        with open(conf, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        if not os.path.exists(turns):
            run(py, tool('osm_turns.py'), pbf, turns, env=env)
        if not os.path.exists(coast):
            run(py, tool('extract_coast.py'), pbf, coast, env=env)

    def cards_step():
        prep()
        cmd = [py, tool('build_gen_lev.py'), '--config', conf, '--unit', unit, '--slug', slug, work, '--reuse',
               '--base', base, '--area', area_arg]
        if B.get('landuse'): cmd += ['--landuse', B['landuse']]
        if B.get('house'): cmd += ['--house']
        cmd += ['--write', args.get('write') or B['write']]
        if B.get('coarse_graph'): cmd += ['--coarse-graph']
        cmd += ['--what', args.get('what') or slug]
        run(*cmd, env=env)
        if not os.path.exists(out):
            raise SystemExit('the build wrote another card than %s; --out must be the next number in %s'
                             % (out, os.path.join(cards, 'builds')))

    def poi():
        os.makedirs(stage, exist_ok=True)
        run(py, tool('poi_apply.py'), '--config', conf, work, out, os.path.join(stage, 'poi-' + unit),
            env=env_v)
        stamp('poi_apply', '%s %s' % (work, out), out)
        os.replace(out, s1)

    def poicap(src, dst, workdir, *extra):
        run(poi_py, tool('build_poicap.py'), '--config', conf, '--area', area_arg, *extra,
            '--osm2clarion', HERE, '--python', poi_py, '--base', src, '--out', dst,
            '--workdir', os.path.join(stage, workdir), env=env_v)

    def icons():
        poicap(s1, s2, 'poicap-' + unit, '--allow-placeholder-refs')
        stamp('build_poicap-icons', '--area %s --allow-placeholder-refs' % area_arg, s2)

    def addr():
        shapes = sorted(glob.glob(os.path.join(work, 'rd', '*.shapes.json')))
        poicap(s2, out, 'addr-' + unit, '--house-ranges', 'ALL', '--all-streets', '--shapes', *shapes,
               '--addresses', cfg['data']['addresses'], '--postcodes', cfg['data']['postcodes'])
        for f in (s1, s2):
            if os.path.exists(f): os.remove(f)
        stamp('build_poicap-addr', '--area %s --house-ranges ALL --all-streets' % area_arg, out)

    def consent():
        cmd = cfg['units'][unit].get('consent_cmd')
        if not cmd:
            return
        cmd = expand(cmd).replace('{img}', out)
        print('\n$', cmd, flush=True)
        subprocess.run(cmd, shell=True, check=True, env=env_v)
        stamp('me0_consent', cfg['units'][unit]['consent_cmd'], out)

    def check():
        run('shasum', '-a', '256', out)
        checker = tool('card_check.py')
        if os.path.exists(checker):
            run(py, checker, '--card', out, *(['--prev', args['prev']] if args.get('prev') else []), env=env)

    return [('prep', prep), ('cards', cards_step), ('poi', poi), ('icons', icons), ('addr', addr),
            ('consent', consent), ('check', check)]


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    args = {}
    flags = ('--base', '--out', '--work', '--osm', '--area', '--local', '--what', '--prev', '--stage',
             '--write', '--from', '--only')
    for flag in flags:
        if flag in argv:
            i = argv.index(flag)
            args[flag[2:]] = argv[i + 1]
            del argv[i:i + 2]
    mode = P.build.get('mode')
    if mode not in ('generate', 'area'):
        raise SystemExit('profile %s sets no [build] mode (generate or area)' % P.name)
    names = GENERATE_STEPS if mode == 'generate' else AREA_STEPS
    if '--list' in argv:
        print('\n'.join(names))
        return
    if argv or 'base' not in args or 'out' not in args:
        raise SystemExit(__doc__)
    base, out = os.path.abspath(expand(args['base'])), os.path.abspath(expand(args['out']))
    work = os.path.abspath(expand(args.get('work') or os.path.join('build', os.path.basename(out)[:-4])))
    os.makedirs(work, exist_ok=True)
    P, prof = profile_with_cells(P, P.path or 'default', base, work)
    if mode == 'area':
        plan = area_steps(P, prof, base, out, work, args.get('area'), args.get('osm'))
    else:
        local = {}
        lp = args.get('local') or P.build.get('local')
        if lp:
            lp = expand(lp)
            lp = lp if os.path.isabs(lp) else os.path.join(os.path.dirname(os.path.dirname(P.path)), lp)
            if os.path.exists(lp):
                import tomllib
                with open(lp, 'rb') as f:
                    local = tomllib.load(f)
            elif args.get('local'):
                raise SystemExit('no such local file: %s' % lp)
        plan = generate_steps(P, prof, base, out, work, args.get('area'), args.get('osm'), local, args)
    if 'only' in args:
        plan = [(n, f) for n, f in plan if n == args['only']] or sys.exit(
            'no step %r (have: %s)' % (args['only'], ', '.join(names)))
    elif 'from' in args:
        if args['from'] not in names: raise SystemExit('no step %r (have: %s)' % (args['from'], ', '.join(names)))
        plan = plan[names.index(args['from']):]
    for name, fn in plan:
        print('\n===== %s' % name, flush=True)
        fn()
    print('\ndone. %s' % out)


if __name__ == '__main__':
    main()
