#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the whole chain for one profile.

    ./build.py --profile iceland --base CARD --out DIR [--target v2014] [--from STEP]

CARD is a mounted base card, or a copy of one: it needs MAPAL001/LEV3456R.DAT,
MAPAL001/<any tile>.DAT as the header template, and MMAIN001.DAT. The card is only read.
DIR gets the files you then copy onto a card: MAPAL001/*.DAT, MMAIN001.DAT and, if the
card is mounted, DRSTR001.DAT.

Each step is one tool, printed as it runs, so a step can be repeated by hand. --from skips
ahead to a named step and --only runs one; `--list` prints the names.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, 'tools')
sys.path.insert(0, TOOLS)
import clarion_profile


STEPS = ['extract', 'coarse', 'background', 'roads', 'master', 'index']


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


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    prof = P.path or 'default'
    base = out = None
    target = P.targets.get('default')
    first = only = None
    for flag, setter in (('--base', 'base'), ('--out', 'out'), ('--target', 'target'),
                         ('--from', 'first'), ('--only', 'only')):
        if flag in argv:
            i = argv.index(flag)
            value = argv[i + 1]
            del argv[i:i + 2]
            if setter == 'base': base = value
            elif setter == 'out': out = value
            elif setter == 'target': target = value
            elif setter == 'first': first = value
            else: only = value
    if '--list' in argv:
        print('\n'.join(STEPS))
        return
    if not base or not out:
        raise SystemExit(__doc__)
    if not P.cells:
        raise SystemExit('profile %s lists no cells' % P.name)
    for d in (out, os.path.join(out, 'MAPAL001'), os.path.join(out, 'work'),
              os.path.join(out, 'work', 'base')):
        os.makedirs(d, exist_ok=True)
    plan = steps(P, prof, base, out, target)
    names = [n for n, _ in plan]
    if only:
        plan = [(n, f) for n, f in plan if n == only] or sys.exit('no step %r (have: %s)' % (only, ', '.join(names)))
    elif first:
        if first not in names: raise SystemExit('no step %r (have: %s)' % (first, ', '.join(names)))
        plan = plan[names.index(first):]
    else:
        plan = plan[1:]      # the extract step needs osmium; ask for it by name
    for name, fn in plan:
        print('\n===== %s' % name, flush=True)
        fn()
    print('\ndone. %s holds the files to copy onto the card.' % out)


if __name__ == '__main__':
    main()
