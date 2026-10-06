#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Give a section-1 level-5 record the road block the unit draws at 64 km.

A level-5 record carries two geometry blocks: the road block at w7/w8 and the background
at w9/w10. Iceland's rebuilt records have only the background, so 64 km draws the coastline
and no roads. This adds the missing block without touching the background or the names
section: the existing record is taken apart at its own offsets, the road block is inserted
in front of the background, and the three pointers are renumbered.

The block is the eleven-pair layout `lev_roads.fa_record` writes, pairs being
(offset in 4-byte units, shape count), with the shapes in layer 6, where every
shipped level-5 record of classes 3 to 6 keeps them.

Works on both cards. The 2017 card keeps the background at w9/w10; the 2014 card was
built with a `clarion_bg` that picked the slot from w2, so its rebuilt records have it at
w7/w8 instead. Either way the background ends up at w9/w10 and the roads at w7/w8.

    ./lev5_roads.py --profile iceland IN.DAT OUT.DAT [roads.json]
    --classes trunk,primary   which OSM classes reach 64 km
    --simplify METRES
"""
import json, os, struct, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lev_coarse, lev_roads, rd_build, clarion_bg, clarion_profile

PW, PH = 8.0, 16 / 3            # level-5 parcel in degrees (8x8 meshes)
LON0, LAT0 = -20.0, 32 / 1.5    # south-west corner of the grid; set from a profile
X0 = 904                        # the same corner in meshes
LAYER = 6                       # class band 3..6 at level 5
TOKEN = 0x0318                  # w20 per-parcel token; shipped records with roads all set one
MAXPTS = 60                     # shipped level-5 shapes top out well under this

CLASS_CODE = {'trunk': 4, 'trunk_link': 4, 'primary': 5, 'primary_link': 5,
              'motorway': 3, 'motorway_link': 3, 'secondary': 6, 'secondary_link': 6}


def use_profile(P):
    global LON0, LAT0, X0
    y0, X0 = P.mesh_origin
    LON0, LAT0 = clarion_bg.mesh_to_lon(X0), clarion_bg.mesh_to_lat(y0)


def chains(ways):
    """Join ways that meet end to end and share a class and a route number into one run.
    OSM splits Route 1 into 1,683 trunk ways; one shape per way would put four times the
    shipped level-5 shape count on the island."""
    out = []
    groups = {}
    for w in ways:
        groups.setdefault((CLASS_CODE.get(w['hw'], 6), w['ref'] or w['name'] or id(w)), []).append(w)
    for (cls, _), gw in groups.items():
        ends = {}
        for i, w in enumerate(gw):
            for p in (tuple(w['nodes'][0]), tuple(w['nodes'][-1])):
                ends.setdefault(p, []).append(i)
        left = set(range(len(gw)))
        while left:
            # start at a way with a free end if there is one, so chains are not cut short
            i = next((i for i in left if any(len(ends[tuple(gw[i]['nodes'][e])]) == 1 for e in (0, -1))), next(iter(left)))
            left.discard(i)
            pts = [tuple(p) for p in gw[i]['nodes']]
            for _ in range(2):
                while True:
                    nxt = [j for j in ends.get(pts[-1], []) if j in left]
                    if not nxt:
                        break
                    j = nxt[0]; left.discard(j)
                    seg = [tuple(p) for p in gw[j]['nodes']]
                    if seg[-1] == pts[-1]:
                        seg.reverse()
                    pts += seg[1:]
                pts.reverse()
            out.append(dict(hw=gw[i]['hw'], nodes=[list(p) for p in pts]))
    return out


def parcel(lon, lat):
    return (int((lat - LAT0) // PH), int((lon - LON0) // PW))


def quant(lon, lat, pc):
    lon0, lat0 = LON0 + pc[1] * PW, LAT0 + pc[0] * PH
    return (max(0, min(2047, round((lon - lon0) / PW * 2048))),
            max(0, min(2047, round((lat - lat0) / PH * 2048))))


def road_block(shapes):
    """The eleven-pair road unit: (offset, shape count) per layer, then the shapes."""
    unit = [0xffff, 0x0000] * 11
    unit[2 * LAYER], unit[2 * LAYER + 1] = 11, len(shapes)
    for s in shapes:
        unit += s
    if len(unit) % 2:
        unit.append(0)
    return struct.pack('>%dH' % len(unit), *unit)


def bg_slot(w):
    """Which pointer pair holds the background. w9/w10 is where it belongs and where the 2017
    card keeps it; the 2014 card was built with a `clarion_bg` that chose the slot from w2, so
    its rebuilt records have it at w7/w8 with w9/w10 zero. Either way the road block goes to
    w7/w8 and the background moves to w9/w10. The checks are the ones this pass needs, over
    the shared rule in `clarion_bg`."""
    gi = clarion_bg.bg_slot(w)
    assert w[gi] not in (0, 0xffff) and w[gi + 1], 'record carries no geometry in either slot'
    if gi == 9:
        assert w[7] == 0xffff, 'record already has a road block at w7/w8'
    else:
        assert w[9] in (0, 0xffff) and not w[10], 'w9/w10 = %#x/%#x is not empty' % (w[9], w[10])
    return gi


def patch_record(plain, shapes):
    """Insert a road block into a level-5 background record, keeping its other sections."""
    w = list(struct.unpack_from('>22H', plain, 0))
    if w[2] != 0xfa:
        return None, None      # kind 0x0fa0: an empty parcel, so there is nothing to add to
    gi = bg_slot(w)
    bg = plain[4 * w[gi]:4 * (w[gi] + w[gi + 1])]
    names = plain[4 * w[11]:4 * (w[11] + w[12])] if w[11] != 0xffff else b''
    for i in (13, 15, 17):
        assert w[i] == 0xffff, 'section at w%d is in use' % i
    road = road_block(shapes)
    w[10] = w[gi + 1]
    w[7], w[8] = 11, len(road) // 4
    w[9] = w[7] + w[8]
    w[11] = (w[9] + w[10]) if names else 0xffff
    w[20] = TOKEN
    out = struct.pack('>22H', *w) + road + bg + names
    assert len(out) == 44 + len(road) + len(bg) + len(names)
    c = zlib.compressobj(6, zlib.DEFLATED, 13, 8)
    comp = c.compress(out) + c.flush()
    if len(comp) % 2:
        comp += b'\0'
    return out, comp


def main():
    argv = sys.argv[1:]
    P = clarion_profile.take_arg(argv)
    use_profile(P)
    classes = {'trunk', 'trunk_link', 'primary', 'primary_link'}
    tol = 400.0
    if '--classes' in argv:
        i = argv.index('--classes'); classes = set(argv[i + 1].split(',')); del argv[i:i + 2]
    if '--simplify' in argv:
        i = argv.index('--simplify'); tol = float(argv[i + 1]); del argv[i:i + 2]
    src, dest = argv[0], argv[1]
    rpath = argv[2] if len(argv) > 2 else P.roads
    if not rpath:
        raise SystemExit('no road set: set source.roads in the profile or pass roads.json')

    ways = chains([w for w in json.load(open(rpath)) if w['hw'] in classes])
    print(f'{len(ways)} runs after joining')
    for w in ways:
        w['nodes'] = rd_build.simplify(w['nodes'], tol)
    runs = lev_roads.runs(ways, PW, PH, parcel, quant, maxpts=MAXPTS)

    d = bytearray(open(src, 'rb').read())
    D = lev_coarse.read_dir(d, 5)
    assert D['box'][2] == X0 and D['cols'] == 11, (D['box'], X0)
    done = 0
    for (row, col), rl in sorted(runs.items()):
        if not (0 <= row < D['rows'] and 0 <= col < D['cols']):
            continue
        s = D['slots'][row]
        old = s['recs'][col]
        if old is None:
            print(f'  r{row}c{col}: no background record, skipped')
            continue
        plain = zlib.decompress(bytes(old))
        shapes = [lev_roads.shape_words_l4(pts, CLASS_CODE.get(hw, 6)) for pts, hw in rl]
        new, comp = patch_record(plain, shapes)
        if new is None:
            print(f'  r{row}c{col}: parcel carries no background shapes, skipped')
            continue
        s['recs'][col] = comp
        s['pairs'][2 * col], s['pairs'][2 * col + 1] = len(comp) // 2, len(new) // 2
        s['dirty'] = True
        done += 1
        print(f'  r{row}c{col}: {len(shapes)} shapes {sum(len(p) for p, _ in rl)} points, '
              f'record {len(plain)} -> {len(new)} bytes ({len(comp)} compressed)')
    lev_coarse.write_dir(d, D)
    open(dest, 'wb').write(d)
    print(f'level-5 road blocks written: {done}; wrote {dest} {len(d)}')


if __name__ == '__main__':
    main()
