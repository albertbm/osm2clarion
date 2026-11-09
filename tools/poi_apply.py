#!/usr/bin/env python3
"""Apply one dealer/POI set to a generated BUILD_DIR, write it onto CARD.img and refresh
DRSTR: a road-tile icon (poi_badge.py), every camera in the config's camera file
(camera_write.py), a building-scale icon (house_badge.py) and a POINT record anchored on
ADDRE (point_add.py).

    poi_apply.py --config PATH BUILD_DIR CARD.img OUT_DIR
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import camera_write  # noqa: E402
import house_badge  # noqa: E402
import point_add  # noqa: E402
import poi_badge  # noqa: E402
from card_pack import wrap  # noqa: E402
from fatedit import Editor  # noqa: E402
import drstr_refresh  # noqa: E402


def sha(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def load_cameras(path, speed_fallback):
    """cameras.json with every speed_limit_kmh filled in - speed_fallback where the camera
    node itself carries none (cameras/README.md: no maxspeed on the camera node itself; the
    record needs a speed field regardless, so this is the nearest OSM way's own maxspeed
    tag, read directly off the PBF by hand)."""
    cams = json.load(open(path))
    for c in cams:
        if c['speed_limit_kmh'] is None:
            c['speed_limit_kmh'] = speed_fallback[c['id']]
    return cams


def pos_arg(t):
    return ','.join(str(x) for x in t)


def run(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=False, help='build config (default: $O2C_CONFIG or $ICELAND_CONFIG)')
    ap.add_argument('build_dir')
    ap.add_argument('card')
    ap.add_argument('out_dir')
    a = ap.parse_args(argv)
    cfg_path = a.config or os.environ.get('O2C_CONFIG') or os.environ.get('ICELAND_CONFIG')
    if not cfg_path:
        raise SystemExit('poi_apply.py needs --config PATH (or $O2C_CONFIG)')
    config = json.load(open(os.path.expanduser(cfg_path)))
    dealer = config['dealer']
    road_tiles = config['road_tiles']

    B, img, out = a.build_dir, a.card, a.out_dir
    os.makedirs(out, exist_ok=True)

    # 1: dealer/POI icon onto the road tile holding the dealer address
    rd_in = B + '/rd/%s.DAT' % dealer['road_tile']
    badge_out = out + '/%s.badge.DAT' % dealer['road_tile']
    poi_badge.main([rd_in, badge_out, '--icon', dealer['icon'],
                    '--pos', pos_arg(dealer['tile_l1']), '--pos', pos_arg(dealer['tile_l2'])])

    # 2: every OSM camera, at level 1 and level 2, over every road tile the build touches
    # (camera_write.py: several can share a parcel). The dealer tile goes in badged;
    # the rest go in as rd_build left them.
    cam_in, cam_out = out + '/cam_in', out + '/cam_out'
    os.makedirs(cam_in, exist_ok=True)
    shutil.copy(badge_out, cam_in + '/%s.DAT' % dealer['road_tile'])
    for name in road_tiles:
        if name != dealer['road_tile']:
            shutil.copy(B + '/rd/%s.DAT' % name, cam_in + '/%s.DAT' % name)
    cameras = load_cameras(os.path.expanduser(config['data']['cameras']), config['camera_speed_fallback'])
    report, missing = camera_write.write_cameras(cameras, cam_in, cam_out)
    assert not missing, 'camera(s) outside the built tiles: %r' % missing
    final_tiles = {}
    for name in road_tiles:
        touched = os.path.join(cam_out, '%s.DAT' % name)
        src = touched if os.path.exists(touched) else os.path.join(cam_in, '%s.DAT' % name)
        final_tiles[name] = out + '/%s.final.DAT' % name
        shutil.copy(src, final_tiles[name])
    print('cameras placed:', {cid: len(hits) for cid, hits in report.items()})

    # 3: building-scale icon
    house_in = B + '/house/' + dealer['house_cell'] + '.DAT'
    house_out = out + '/' + dealer['house_cell'] + '.final.DAT'
    house_badge.main([house_in, house_out, '--lon', str(dealer['lon']), '--lat', str(dealer['lat']),
                      '--icon', dealer['icon']])

    # 4: dealer POINT record, ADDRE-anchored
    point_plain = B + '/addr/POINT001.DAT'
    addre_plain = B + '/addr/ADDRE001.DAT'
    pcmp, pind = wrap(open(point_plain, 'rb').read())
    acmp, aind = wrap(open(addre_plain, 'rb').read())
    open(out + '/POINT001.in.cmp', 'wb').write(pcmp)
    open(out + '/POINT001.in.ind', 'wb').write(pind)
    open(out + '/ADDRE001.cmp', 'wb').write(acmp)
    open(out + '/ADDRE001.ind', 'wb').write(aind)
    dealer_out = out + '/dealer_out'
    os.makedirs(dealer_out, exist_ok=True)
    point_add.main([out + '/POINT001.in.cmp', out + '/POINT001.in.ind', dealer_out,
                    str(dealer['lat']), str(dealer['lon']),
                    '--category', str(dealer['category']), '--name', dealer['name'],
                    '--street', dealer['street'], '--town', dealer['town'], '--house', dealer['house'],
                    '--addre', out + '/ADDRE001.cmp', out + '/ADDRE001.ind', '--data-id', '1'])

    # 5: write onto the card
    point_size = os.path.getsize(dealer_out + '/POINT001.DAT')
    ed = Editor(img)
    for name, path in final_tiles.items():
        ed.write('MAPAL001', '%s.DAT' % name, open(path, 'rb').read())
    ed.write('HOUSE001', dealer['house_cell'] + '.DAT', open(house_out, 'rb').read())
    ed.write('REFER001', 'POINT001.DAT.cmp', open(dealer_out + '/POINT001.DAT.cmp', 'rb').read())
    ed.write('REFER001', 'POINT001.DAT.ind', open(dealer_out + '/POINT001.DAT.ind', 'rb').read())
    ed.close()

    # 6: refresh DRSTR for every changed entry
    refresh = {('MAPAL001', '%s.DAT' % name): None for name in final_tiles}
    refresh[('HOUSE001', dealer['house_cell'] + '.DAT')] = None
    refresh[('REFER001', 'POINT001.DAT')] = point_size
    sizes, free = drstr_refresh.refresh(img, refresh)
    print('drstr refreshed:', sizes, 'free', free)
    for p in list(final_tiles.values()) + [house_out, dealer_out + '/POINT001.DAT.cmp', dealer_out + '/POINT001.DAT.ind']:
        print(sha(p), p)


if __name__ == '__main__':
    run(sys.argv[1:])
