#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a card from a profile.

    ./build.py --profile NAME --base CARD --out OUT.img [--work DIR] [--osm PBF] [--area NAME,...]
               [--local FILE] [--what TEXT] [--prev IMG] [--stage DIR]
               [--map-from CARD | --restamp-from CARD] [--only STEP | --from STEP] [--list]

Everything specific to a region, a unit or a data source is in profiles/NAME.toml. [build] mode
picks the chain:

  generate  A card from scratch on the unit's base card: background, roads, coarse file,
            building-scale map, addresses and POI icons for the profile's areas
            (build_gen_lev.py, poi_apply.py, build_poicap.py). OUT is
            CARDS/builds/<prefix>-<number>-<unit>-<slug>.img; the unit and slug are read from
            that name, and CARDS is the directory two levels up.
  area      The base card keeps its own map. Its roads are stripped and one area's roads are
            rebuilt from OSM. --base is the card image, only read. The factory step clears
            the work dir's copies of an earlier base; later steps refuse to run on them.

The card is written as OUT.partial (in generate mode with its manifest as MANIFEST.partial),
and renamed, with the cards log row, only once the check step passes. A run that starts the card again
removes a .partial left by an earlier run and says so; a finished OUT is never overwritten.

--work holds everything generated (default build/<name of OUT>). The profile's `local` file, or
--local, is merged over the profile table by table; it holds paths and values that belong to one
machine. Each step prints the command it runs, so a step can be repeated by hand. --list names
the steps, --only runs one and --from resumes at one.
"""
import glob, hashlib, json, os, re, shlex, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, 'tools')
sys.path.insert(0, TOOLS)
import cardcopy
import clarion_profile

GENERATE_STEPS = ['prep', 'cards', 'poi', 'icons', 'addr', 'consent', 'check']
AREA_STEPS = ['factory', 'osm', 'strip', 'roads', 'coarse', 'sec4', 'master', 'card', 'check']


def run(*args, env=None):
    cmd = [str(a) for a in args]
    if cmd[0].endswith('.py'): cmd.insert(0, sys.executable)
    shown = ['python3' if c == sys.executable else os.path.relpath(c, HERE) if c.startswith(HERE) else c
             for c in cmd]
    print('\n$', ' '.join(shown), flush=True)
    subprocess.run(cmd, check=True, env=env)


def tool(name):
    return os.path.join(TOOLS, name)


def pins():
    """{library: version} pinned in pyproject.toml."""
    import tomllib
    with open(os.path.join(HERE, 'pyproject.toml'), 'rb') as f:
        deps = tomllib.load(f)['project']['dependencies']
    return dict(d.split('==') for d in deps if '==' in d)


def need_deps():
    """The steps run under this interpreter, so it must have the pinned library versions."""
    from importlib import metadata
    off = []
    for name, want in sorted(pins().items()):
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        if have != want:
            off.append('%s %s (pinned %s)' % (name, have or 'missing', want))
    if off:
        raise SystemExit('this interpreter has %s; run this as `uv run build.py ...`, which installs '
                         'the versions in uv.lock' % ', '.join(off))


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

    base_id = j('factory', '.base')

    def base_key():
        st = os.stat(base)
        return '%s %d %d' % (base, st.st_size, st.st_mtime_ns)

    def same_base():
        """Steps after factory read its copies, which must be of this --base."""
        try:
            with open(base_id) as f:
                ok = f.read() == base_key()
        except OSError:
            ok = False
        if not ok:
            raise SystemExit('%s holds no copy of %s; run from the factory step' % (fac, base))

    def factory():
        # files of an earlier base must not reach this card
        for d in (fac, outmap):
            if os.path.isdir(d):
                shutil.rmtree(d)
        os.makedirs(outmap)
        card_files(base, fac)
        with open(base_id, 'w') as f:
            f.write(base_key())

    def osm_step():
        pbf = need_osm()
        run(tool('osm_turns.py'), pbf, turns_all)
        run(tool('osm_roads.py'), pbf, roads, '--public', '--relations', ','.join(map(str, A['relations'])),
            '--turns', turns_all, turns, '--ramp-names')

    def strip():
        same_base()
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
        same_base()
        run(tool('make_mmain.py'), '--profile', prof, os.path.join(fac, 'MMAIN001.DAT'), mmain, *tiles(),
            '--lev', lev_out)

    part = out + '.partial'
    written = lambda: [('MAPAL001/' + os.path.basename(t), t) for t in tiles()] + \
        [('MAPAL001/LEV3456R.DAT', lev_out), ('MMAIN001.DAT', mmain)]

    def card_step():
        if not os.path.isfile(base):
            raise SystemExit('--base must be a card image')
        if os.path.exists(out):
            raise SystemExit('%s already exists' % out)
        if os.path.exists(part):
            os.remove(part)
            print('removed a stale file from an earlier run:', part)
        same_base()
        # a fixed write time, so two runs stamp the image alike; set SOURCE_DATE_EPOCH to override
        os.environ.setdefault('SOURCE_DATE_EPOCH', '946684800')
        cardcopy.clone(base, part)
        run(tool('fatwrite.py'), part, '--write', *['%s=%s' % (p.replace('/', ':') if '/' in p else ':' + p, f)
                                                    for p, f in written()])
        run(tool('drstr_refresh.py'), part, *['MAPAL001:' + os.path.basename(t) for t in tiles()],
            'MAPAL001:LEV3456R.DAT')

    def check():
        """The card is renamed to OUT only when every file reads back as built and the index,
        MMAIN and DATA VERSION agree."""
        import card_verify, fatread
        if not os.path.exists(part):
            raise SystemExit('no card in progress (%s); run from the card step' % part)
        want = {p: cardcopy.sha256(f) for p, f in written()}
        with fatread.Fat32(part) as fs:
            got = {p.strip('/'): hashlib.sha256(fs.f.read_clusters(c, sz)).hexdigest()
                   for p, sz, c, _ in fs.walk() if sz is not None and p.strip('/') in want}
        bad = sorted(p for p in want if got.get(p) != want[p])
        if bad:
            raise SystemExit('check failed: %s do not read back as built' % ', '.join(bad))
        print('%d written files read back as built' % len(want))
        versions = set()
        for n in os.listdir(fac):
            with open(os.path.join(fac, n), 'rb') as f:
                versions |= {m.group(1).decode('utf-16-be') for m in card_verify.STAMP.finditer(f.read())}
        if len(versions) != 1:
            raise SystemExit('check failed: the base card carries %d DATA VERSIONs: %s'
                             % (len(versions), ', '.join(sorted(versions))))
        run(tool('card_verify.py'), '--card', part, '--version', versions.pop(), '--skip', 'cmp_lst')
        os.replace(part, out)
        print('checked; card renamed to', out)

    return [('factory', factory), ('osm', osm_step), ('strip', strip), ('roads', roads_step),
            ('coarse', coarse), ('sec4', sec4), ('master', master), ('card', card_step), ('check', check)]


def consent_ok(code, output):
    """The consent tool exits 1 and says "already patched" for a card that carries the patch;
    anything else but 0 is a failure."""
    return code == 0 or (code == 1 and 'already patched' in output)


# the post steps of the full chain, appended to the manifest only when each one succeeds
CHAIN = ('poi_apply', 'build_poicap-icons', 'build_poicap-addr')


def chain_problems(m, card_sha, full):
    """Why the card in progress is not what the last step of its chain wrote, or []."""
    post = m.get('post_steps', [])
    names = [s['name'] for s in post]
    bad = ['%s never finished' % n for n in CHAIN if full and n not in names]
    if post and post[-1]['image_sha256'] != card_sha:
        bad.append('the card is not the image %s wrote' % post[-1]['name'])
    return bad


def make_config(P, local, cells, base, osm, work):
    """The JSON config the generator tools read, from the profile and the local file."""
    raw = merge(P.raw, local)
    B = raw.get('build', {})
    if 'poi_python' in B:
        raise SystemExit('[build] poi_python is gone: every step runs under the interpreter running '
                         'build.py; remove it from the profile or the local file')
    root = os.path.dirname(os.path.dirname(P.path))

    def path(p):
        p = expand(p)
        return p if os.path.isabs(p) or '$' in p else os.path.join(root, p)

    def need(table, key=None):
        """A table the generate chain cannot run without, or a clear exit naming it."""
        t = raw.get(table)
        if key is not None:
            t = (t or {}).get(key)
        if t is None:
            raise SystemExit('profile %s (with its local file) has no %s' % (
                P.name, '[%s] %s' % (table, key) if key else '[%s] table' % table))
        return t

    data = {k: path(v) for k, v in need('data').items()}
    if osm: data['pbf'] = os.path.abspath(osm)
    units = {}
    for u, c in need('units').items():
        units[u] = dict(c, icode=c.get('icode') or os.path.abspath(base))
    cfg = dict(
        units=units, cells=cells, sec4_cells=need('build', 'sec4_cells'),
        areas={n: dict(relations=a['relations'], cells=a['cells'], bbox=a['bbox']) for n, a in need('areas').items()},
        dealer=raw.get('dealer'), road_tiles=need('build', 'road_tiles'), data=data,
        camera_speed_fallback=raw.get('camera_speed_fallback', {}), card_naming=need('card_naming'),
        cards_log=need('build', 'cards_log'), poicap=need('poicap'),
        roads_poc=need('roads_poc'), addr=need('addr'), allow_leftovers=B.get('allow_leftovers', []),
        profile=P.path)
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
    number, unit, slug = int(m.group(1)), m.group(2), m.group(3)
    cards = os.path.dirname(os.path.dirname(out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    manifest = out[:-4] + '.manifest.json'
    # the card is built under .partial names and renamed only after the check step passes
    part, mpart = out + '.partial', manifest + '.partial'
    area_arg = areas or ','.join(cfg['areas'])
    stage = args.get('stage') or os.path.join(work, 'stage')
    version = cfg['units'][unit]['version']
    conf = os.path.join(work, 'config', P.name + '.json')
    env = dict(os.environ, O2C_CONFIG=conf, CARDS_DIR=cards, OSM2CLARION=HERE,
               SOURCE_DATE_EPOCH=os.environ.get('SOURCE_DATE_EPOCH', '946684800'))
    py = sys.executable
    # the address and POI steps stamp the unit's DATA VERSION into what they write
    env_v = dict(env, CLARION_DATA_VERSION=version)
    s1, s2 = (os.path.join(stage, '%s-%s.img' % (k, unit)) for k in ('s1', 's2'))

    def stamp(name, cmd, target):
        run(py, tool('buildstamp.py'), 'post-step', mpart, name, cmd, target, env=env_v)

    def prep():
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        with open(conf, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)

    def cards_step():
        prep()
        cmd = [py, tool('build_gen_lev.py'), '--config', conf, '--unit', unit, '--slug', slug, work, '--reuse',
               '--number', str(number), '--base', base, '--area', area_arg]
        if B.get('landuse'): cmd += ['--landuse', B['landuse']]
        if B.get('house'): cmd += ['--house']
        cmd += ['--write', args.get('write') or B['write']]
        if B.get('coarse_graph'): cmd += ['--coarse-graph']
        cmd += ['--what', args.get('what') or slug, '--partial']
        run(*cmd, env=env)
        if not os.path.exists(part):
            raise SystemExit('the build wrote another card than %s' % part)

    def fresh(name):
        """A stage dir emptied first, so nothing from an earlier build reaches this card."""
        d = os.path.join(stage, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        return d

    def poi():
        os.makedirs(stage, exist_ok=True)
        fresh('poi-' + unit)
        run(py, tool('poi_apply.py'), '--config', conf, work, part, os.path.join(stage, 'poi-' + unit),
            env=env_v)
        stamp('poi_apply', '%s %s' % (work, out), part)
        shutil.move(part, s1)    # the stage dir may be on another volume than the card

    def poicap(src, dst, workdir, *extra):
        run(py, tool('build_poicap.py'), '--config', conf, '--area', area_arg, *extra,
            '--osm2clarion', HERE, '--base', src, '--out', dst,
            '--workdir', fresh(workdir), env=env_v)

    def icons():
        poicap(s1, s2, 'poicap-' + unit, '--allow-placeholder-refs')
        stamp('build_poicap-icons', '--area %s --allow-placeholder-refs' % area_arg, s2)

    def addr():
        shapes = sorted(glob.glob(os.path.join(work, 'rd', '*.shapes.json')))
        poicap(s2, part, 'addr-' + unit, '--house-ranges', 'ALL', '--all-streets', '--shapes', *shapes,
               '--addresses', cfg['data']['addresses'], '--postcodes', cfg['data']['postcodes'])
        for f in (s1, s2):
            if os.path.exists(f): os.remove(f)
        stamp('build_poicap-addr', '--area %s --house-ranges ALL --all-streets' % area_arg, part)

    def consent():
        cmd = cfg['units'][unit].get('consent_cmd')
        if not cmd:
            return
        cmd = expand(cmd).replace('{img}', shlex.quote(part))
        print('\n$', cmd, flush=True)
        r = subprocess.run(cmd, shell=True, env=env_v, capture_output=True, text=True)
        print(r.stdout + r.stderr, end='')
        if not consent_ok(r.returncode, r.stdout + r.stderr):
            raise SystemExit('consent step failed (exit %d)' % r.returncode)
        stamp('me0_consent', cfg['units'][unit]['consent_cmd'], part)

    def map_step():
        prep()
        run(py, tool('build_gen_lev.py'), '--config', conf, '--unit', unit, '--slug', slug, work,
            '--number', str(number), '--base', base, '--map-from', args['map-from'],
            '--what', args.get('what') or slug, '--partial', env=env)
        if not os.path.exists(part):
            raise SystemExit('the build wrote another card than %s' % part)

    def restamp_step():
        prep()
        run(py, tool('build_gen_lev.py'), '--config', conf, '--unit', unit, '--slug', slug, work,
            '--number', str(number), '--base', base, '--restamp-from', args['restamp-from'],
            '--what', args.get('what') or slug, '--partial', env=env)
        if not os.path.exists(part):
            raise SystemExit('the build wrote another card than %s' % part)

    def check():
        prep()      # card_verify reads the register through the config
        import buildstamp
        buildstamp.set_fields(mpart, deps=buildstamp.deps())
        card_sha = cardcopy.sha256(part)
        print(card_sha, part)
        with open(mpart) as f:
            bad = chain_problems(json.load(f), card_sha, not (args.get('map-from') or args.get('restamp-from')))
        if bad:
            raise SystemExit('check failed: ' + '; '.join(bad))
        run(py, tool('card_check.py'), '--card', part, '--cells', ','.join(cfg['road_tiles']),
            *(['--prev', args['prev']] if args.get('prev') else []), env=env)
        groups = (args.get('write') or B['write']).split(',')
        towns = ['--towns'] if args.get('map-from') or args.get('restamp-from') or 'addr' in groups else []
        run(py, tool('card_verify.py'), '--card', part, '--version', version, *towns, '--manifest', mpart,
            '--allow-new', *cfg['allow_leftovers'], env=env)
        os.replace(mpart, manifest)
        os.replace(part, out)
        print('checked; card renamed to', out)
        import cards_log
        cards_log.add(os.path.join(cards, cfg['cards_log']), number, unit, slug, args.get('what') or slug,
                      base, cards)

    def clear_stale(plan_names):
        """A run that makes the card refuses a finished one and removes a half-made one."""
        if not {'cards', 'map', 'restamp'} & set(plan_names):
            if not any(os.path.exists(p) for p in (part, s1, s2)):
                raise SystemExit('no card in progress (%s); run from the start' % part)
            return
        if os.path.exists(out):
            raise SystemExit('%s already exists; pick the next number' % out)
        for p in (part, mpart, s1, s2):
            if os.path.exists(p):
                os.remove(p)
                print('removed a stale file from an earlier run:', p)

    if args.get('map-from'):
        return [('prep', prep), ('map', map_step), ('consent', consent), ('check', check)], clear_stale
    if args.get('restamp-from'):
        return [('prep', prep), ('restamp', restamp_step), ('consent', consent), ('check', check)], clear_stale
    return [('prep', prep), ('cards', cards_step), ('poi', poi), ('icons', icons), ('addr', addr),
            ('consent', consent), ('check', check)], clear_stale


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    args = {}
    flags = ('--base', '--out', '--work', '--osm', '--area', '--local', '--what', '--prev', '--stage',
             '--write', '--map-from', '--restamp-from', '--from', '--only')
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
        short = [k for k in ('map-from', 'restamp-from') if k in args]
        if short and mode == 'generate':
            names = ['prep', short[0].split('-')[0], 'consent', 'check']
        print('\n'.join(names))
        return
    if argv or 'base' not in args or 'out' not in args:
        raise SystemExit(__doc__)
    need_deps()
    base, out = os.path.abspath(expand(args['base'])), os.path.abspath(expand(args['out']))
    work = os.path.abspath(expand(args.get('work') or os.path.join('build', os.path.basename(out)[:-4])))
    os.makedirs(work, exist_ok=True)
    P, prof = profile_with_cells(P, P.path or 'default', base, work)
    before = None
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
        plan, before = generate_steps(P, prof, base, out, work, args.get('area'), args.get('osm'), local, args)
    names = [n for n, _ in plan]    # --map-from and --restamp-from run a shorter chain
    if 'only' in args:
        plan = [(n, f) for n, f in plan if n == args['only']] or sys.exit(
            'no step %r (have: %s)' % (args['only'], ', '.join(names)))
    elif 'from' in args:
        if args['from'] not in names: raise SystemExit('no step %r (have: %s)' % (args['from'], ', '.join(names)))
        plan = plan[names.index(args['from']):]
    if before:
        before([n for n, _ in plan])
    for name, fn in plan:
        print('\n===== %s' % name, flush=True)
        fn()
    print('\ndone. %s' % out)


if __name__ == '__main__':
    main()
