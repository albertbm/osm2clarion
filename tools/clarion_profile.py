#!/usr/bin/env python3
"""Read a build profile.

Everything specific to a region lives in a TOML profile, so the tools stay general:
the grid cells, the database box, the west shift a region outside the shipped box
needs, the sea label and the road settings. See profiles/iceland.toml.

    import clarion_profile
    P = clarion_profile.load('iceland')      # a name, or a path to a .toml

Every tool takes --profile and falls back to its own defaults without one, so a single
step can still be run by hand.
"""
import os, sys, tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The box the European cards ship with: lat 21.33..80, lon -20..68, in arc-minutes.
EUROPE_BOUNDS = (4800, 1280, -1200, 4080)

L2CLASSES_NARROW = {'motorway', 'trunk', 'primary', 'motorway_link', 'trunk_link', 'primary_link'}
# Shipped level 2 also carries secondary and tertiary as class 7, and that is what fills the
# 500 m to 2 km view in a town: 7,541 of the 18,632 level-2 shapes in the 2014 card's
# south-west Norway tile are class 7.
L2CLASSES_WIDE = L2CLASSES_NARROW | {'secondary', 'secondary_link', 'tertiary', 'tertiary_link'}

COARSE_CLASSES = {'motorway', 'trunk', 'primary', 'motorway_link', 'trunk_link', 'primary_link'}

# Smallest island kept and simplify tolerance per level, in square degrees and degrees.
# The shipped map shows skerries only close in, so the coarse levels drop them.
LAND = {6: (0.001, 0.02), 5: (0.001, 0.01), 4: (0.0002, 0.003), 3: (0.00003, 0.001)}


def mesh_origin(bounds):
    """South-west mesh (y, x) of a database box. A mesh is 1 deg of longitude by 40'."""
    return round(bounds[1] / 40), 924 + round(bounds[2] / 60)


class Profile:
    """A region's settings, with the general defaults filled in."""

    def __init__(self, data, path=None):
        self.path = path
        self.name = data.get('name') or os.path.basename(path or '').removesuffix('.toml')
        self.description = data.get('description', '')

        src = data.get('source', {})
        self.osm = self.resolve(src.get('osm'))
        self.roads = self.resolve(src.get('roads'))
        self.coastline = self.resolve(src.get('coastline'))

        grid = data.get('grid', {})
        self.cells = list(grid.get('cells', []))
        self.bounds = tuple(grid['bounds']) if 'bounds' in grid else EUROPE_BOUNDS
        self.bbox = grid.get('bbox')

        shift = data.get('shift', {})
        self.shift = bool(shift.get('enabled', False))
        self.west_meshes = int(shift.get('west_meshes', 0)) if self.shift else 0

        rd = data.get('roads', {})
        self.simplify_metres = float(rd.get('simplify_metres', 0))
        self.skip = set(rd.get('skip', []))
        self.l2_graph = bool(rd.get('l2_graph', False))
        self.l2_classes = l2_classes(rd.get('l2_classes', 'wide'))
        self.coarse_classes = set(rd.get('coarse_classes', COARSE_CLASSES))
        self.coarse_simplify_metres = float(rd.get('coarse_simplify_metres', 20))

        land = data.get('land', {})
        self.land = {}
        for level in (6, 5, 4, 3):
            e = land.get('level_%d' % level)
            self.land[level] = (e['min_area'], e['simplify']) if e else LAND[level]

        lab = data.get('labels', {})
        self.sea = lab.get('sea', '')
        self.coarse_labels = lab.get('coarse', '')

        self.targets = data.get('targets', {})

    @property
    def mesh_origin(self):
        return mesh_origin(self.bounds)

    def target(self, name):
        if name not in self.targets:
            raise SystemExit('profile %s has no target %r (it has: %s)'
                             % (self.name, name, ', '.join(self.targets) or 'none'))
        return self.targets[name]

    def resolve(self, value):
        """A path from the profile, read relative to the repository root."""
        if value is None or self.path is None or os.path.isabs(value):
            return value
        return os.path.normpath(os.path.join(os.path.dirname(self.path), '..', value))

    def __repr__(self):
        return '<Profile %s cells=%d bounds=%s west=%s>' % (
            self.name, len(self.cells), self.bounds, self.west_meshes or 'none')


def l2_classes(spec):
    """A set of OSM highway values from "wide", "narrow", a list or a comma string."""
    if isinstance(spec, list):
        return set(spec)
    if spec == 'narrow':
        return set(L2CLASSES_NARROW)
    if spec == 'wide':
        return set(L2CLASSES_WIDE)
    return set(spec.split(','))


def default():
    """No region: the shipped European box, no cells, no shift."""
    return Profile({})


def load(name):
    """profiles/<name>.toml, or the file at the given path."""
    path = name if os.path.exists(name) else os.path.join(ROOT, 'profiles', name + '.toml')
    if not os.path.exists(path):
        raise SystemExit('no such profile: %s' % name)
    with open(path, 'rb') as f:
        return Profile(tomllib.load(f), os.path.abspath(path))


def take_arg(argv, flag='--profile'):
    """Pull --profile NAME out of argv in place; the default profile without one."""
    if flag in argv:
        i = argv.index(flag)
        name = argv[i + 1]
        del argv[i:i + 2]
        return load(name)
    return default()


if __name__ == '__main__':
    P = load(sys.argv[1] if len(sys.argv) > 1 else 'iceland')
    print(P)
    for k in ('description', 'coastline', 'roads', 'cells', 'bounds', 'mesh_origin',
              'west_meshes', 'sea', 'simplify_metres', 'skip', 'l2_classes', 'l2_graph',
              'coarse_classes', 'land', 'targets'):
        print('%-22s %s' % (k, getattr(P, k)))
