"""Area 0's address files, written from Staðfangaskrá with no bytes taken from a factory card.

Every byte comes from the register, from a decoded field, from the road tiles' shape index,
or from a format constant in frames.py / refer_header.py. The build runs it with --area;
without it, it writes a test card of one town and the streets in [addr] poc_streets:

    ADDRE001.DAT   street search: the test streets, ladder (0, 0, 0), each pointing at one link
                   of its own drawn shape in the level-1 road records
    ZCODE001.DAT   postcode search: every street's postcode -> (0, 0, 0)
    POINT001.DAT   the FACILITY frame with one settlement, REYKJAVÍK, and the ADM frame
                   the City keyboard reads
    INTER001.DAT   empty: no junction data, the same 0-byte family the card has booted with
    COORD001.DAT   REFER001: one row, ordinal 0, Iceland's box; REFER002: no rows, since
                   no REFER002 POINT file exists for Iceland

A street record's lat/lon words are the parcel code of the 0x00fa record holding the
chosen link, and its two references the word offsets of the link's end points from that
record's geometry unit. The shape index comes from rd_build.py
--shape-index. The ladder is (country 0, REYKJAVÍK 0, its settlement 0), which is where
build_icode_clean.py puts the town. Display names keep their accents; search keys are
folded to ASCII, Þ to TH and Ð to D.

    python3 tools/addr/build_addr_area0.py <out dir> --shapes TILE.shapes.json [...] [--area NAME[,NAME...]]

--area writes the named areas instead: for example, capital includes every Staðfangaskrá address of Reykjavík
(SVFNR 0000), Kópavogur (1000), Seltjarnarnes (1100), Garðabær (1300), Hafnarfjörður (1400)
and Mosfellsbær (1604). One ADDRE record per (street, town), the town from the postcode
register, the street's odd and even house numbers as two ranges, ladder (0, town, 0) with
the town's row in build_icode_clean.TOWNS. Each record points at the link of the street's
own drawn shape nearest its address centroid; a street with no road of its name within
NAME_REACH points at the nearest road of any name within ACCESS_REACH, as factory
localities point at their access road; one with neither is left out and listed in
addr_skipped.json. ZCODE carries every postcode of those addresses, POINT one settlement
per town and the ADM frame a municipality row and a settlement row per town.

--only keeps the named streets in ADDRE; ZCODE and POINT keep every town and postcode. In the emulator
the HMI froze at street selection or OK for any record whose street record sits past the
first 64 KiB of ADDRE001 (measured 19 Oct 2026), so the cards carry a short list.
"""
import argparse
import collections
import json
import math
import os
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get('OSM2CLARION', str(Path(__file__).resolve().parent.parent)) + '/tools')
import addre_io  # noqa: E402
import frames  # noqa: E402
import iceland_card  # noqa: E402
import point_io  # noqa: E402
import refer_header  # noqa: E402
import shape_index  # noqa: E402
import zcode_io  # noqa: E402
from poi_point import word_starts  # noqa: E402
from build_icode_clean import TOWNS  # noqa: E402
from addr_csv import addresses, towns  # noqa: E402

AREA = 0
NLEVEL = 3
LADDER = (AREA, 0, 0)


def _config():
    cfg_path = os.environ.get('O2C_CONFIG')
    if not cfg_path:
        raise SystemExit('build_addr_area0 needs O2C_CONFIG set to a build config')
    return json.load(open(cfg_path))


_cfg = _config()
TOWN = _cfg['addr']['poc_town']
# the single-street test build only; --area builds never read these two
STREETS = _cfg['addr'].get('poc_streets', {})       # street -> its postcode in the address register
# the link each record points at is the one nearest this house number, or the street's
# address centroid where None
ANCHOR = _cfg['addr'].get('poc_anchor', {})
FOLD_IS = {'Þ': 'TH', 'þ': 'th', 'Ð': 'D', 'ð': 'd'}
LANG_OTHER = 20      # ADM's local-language code where it is none of the 18 menu
                     # languages: Andorra, Belarus and POINT020 write their own names at 20
# POINT's reference words are not decoded for settlements; they stay above every value
# measured on factory files (33..16,859), to fail rather than resolve to a wrong link.
REF_BASE = 0x8000
ICELAND = tuple(_cfg['addr']['poc_box'])     # lat min, lon min, lat max, lon max


def boxword_lat(lat):
    # COORD001's box, as clean2 wrote it and the card has booted with; not a parcel code
    return round((2048 + 12 * lat) * 16)


def boxword_lon(lon):
    return round((3296 + 8 * lon) * 16)


def keyunits(s, kw):
    out = bytearray()
    for ch in s:
        b = ch.encode('utf-8')
        assert len(b) <= kw, s
        out += b + b'\0' * (kw - len(b))
    return bytes(out)


def fold(name):
    k = addre_io.fold_key(name, FOLD_IS).upper()
    assert k.isascii(), (name, k)
    return k


def word_keys(name, kw):
    """One key per word start, the whole remainder from it, and whether it is a later word."""
    s = fold(name)
    starts = [0] + [j + 1 for j, c in enumerate(s) if c == ' ' and j + 1 < len(s)]
    return [(keyunits(s[p:], kw), p > 0) for p in starts if s[p].isalnum()]


def point_word_keys(name, kw):
    """word_keys by the factory's POINT rule, which poi_point checks the ADM frame against."""
    s = fold(name)
    return [(keyunits(s[p:], kw), p > 0) for p in word_starts(s)]


def house_items(nums):
    odd = sorted(n for n in nums if n % 2)
    even = sorted(n for n in nums if not n % 2)
    out = b''
    if odd:
        out += addre_io.house_item(odd[0], odd[-1], addre_io.SIDE_ODD)
    if even:
        out += addre_io.house_item(even[0], even[-1], addre_io.SIDE_EVEN)
    return out


def streets():
    rows = {s: [] for s in STREETS}
    for r in addresses():
        if r['street'] in rows and r['post'] == STREETS[r['street']]:
            rows[r['street']].append(r)
    out = []
    for s in STREETS:
        rs = rows[s]
        assert rs, s
        nums = sorted({int(r['num']) for r in rs if r['num'].isdigit() and int(r['num'])})
        at = [r for r in rs if r['num'] == str(ANCHOR[s]) and not r['letter']] if ANCHOR[s] else rs
        assert at, (s, ANCHOR[s])
        low = [r for r in rs if r['num'] == str(nums[0])]
        out.append(dict(street=s, name=s.upper(), nums=nums,
                        lat=sum(r['lat'] for r in rs) / len(rs), lon=sum(r['lon'] for r in rs) / len(rs),
                        anchor=(sum(r['lon'] for r in at) / len(at), sum(r['lat'] for r in at) / len(at)),
                        low=(low[0]['lon'], low[0]['lat'])))
    return sorted(out, key=lambda e: fold(e['name']))


def metres(a, b):
    m = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111320 * m, (b[1] - a[1]) * 111320)


def seg_dist(p, a, b):
    kx = 111320 * math.cos(math.radians(p[1])); ky = 111320
    ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky
    bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    L = bx * bx + by * by
    t = 0 if L == 0 else max(0, min(1, -(ax * bx + ay * by) / L))
    return math.hypot(ax + bx * t, ay + by * t)


def links_of(records, street):
    """(record, shape, link, points) for every link of a shape labelled with the street.
    A link's points run from the previous link's end (or the shape's start) to its own end."""
    for R in records:
        for S in R['shapes']:
            if shape_index.label_name(S['label']).upper() != street.upper():
                continue
            pts = S['points']
            for L in S['links']:
                js = [j for j, P in enumerate(pts) if P['link'] == L['id']]
                j0 = js[0] if pts[js[0]]['role'] == 'start' else js[0] - 1
                yield R, S, L, pts[j0:js[-1] + 1]


def street_link(records, e):
    """The record and refs for one street: the link nearest the anchor, its K end the one
    nearer the street's lowest house number."""
    def d(run):
        return min(seg_dist(e['anchor'], (a['lon'], a['lat']), (b['lon'], b['lat'])) for a, b in zip(run, run[1:]))
    cands = [(d(run), R, S, L, run) for R, S, L, run in links_of(records, e['street'])]
    assert cands, e['street']
    dist, R, S, L, run = min(cands, key=lambda c: c[0])
    first, last = run[0], run[-1]
    if metres(e['low'], (last['lon'], last['lat'])) < metres(e['low'], (first['lon'], first['lat'])):
        first, last = last, first
    k, i = first['word'] - R['unit'], last['word'] - R['unit']
    refs = addre_io.street_refs(k, i, first['role'] != 'vertex', last['role'] != 'vertex')
    mid = ((run[0]['lat'] + run[1]['lat']) / 2, (run[0]['lon'] + run[1]['lon']) / 2)
    assert addre_io.parcel_words(*mid) == tuple(R['code']), (e['street'], R['code'], addre_io.parcel_words(*mid))
    return dict(street=e['name'], la=R['code'][0], lo=R['code'][1], refs=refs, shape_at=S['at'],
                link=L['id'], way=L['way'], points=len(run), anchor_m=round(dist, 1))


def ladder_first(srecs):
    """The unit reads a record's ladder from the 10 bytes before its row, so each row
    carries the next row's ladder; the first ladder goes in front of the array (pre)."""
    pre = srecs[0][12:22]
    rows = [r[:12] + n[12:22] for r, n in zip(srecs, srecs[1:])] + [srecs[-1][:12] + b'\0' * 10]
    return rows, pre


def build_addre(ents, kw=1):
    cr = 8 + kw + (4 if kw == 2 else 3)
    names = []
    for e in ents:
        L = e['link']
        names.append(addre_io.name_record(e['name'], L['la'], L['lo'], LADDER, house_items(e['nums']),
                                          refs=L['refs']))
    bykey = {}
    for i, e in enumerate(ents):
        for u, later in word_keys(e['name'], kw):
            bykey.setdefault(u, []).append((i, 0x4000 if later else 0))
    keys, srecs = [], []
    for u in sorted(bykey):
        hits = sorted(bykey[u])
        keys.append((u, len(srecs), len(hits)))
        for i, fl in hits:
            srecs.append(addre_io.street_record(fl, i, LADDER))
    srecs, pre = ladder_first(srecs)
    d, w = frames.addre_hints(names)
    out, blocks = addre_io.assemble(frames.addre_prefix(iceland_card.BOUNDS, iceland_card.VERSION), keys, srecs, names, frames.ADDRE_DF,
                                    pre, cr, kw, AREA, NLEVEL - 2, maxkey_hint=d, namewidth_hint=w)
    return out, keys


def build_zcode():
    codes = sorted({str(p) for p in STREETS.values()})
    return zcode_io.assemble(frames.zcode_prefix(AREA, iceland_card.BOUNDS, iceland_card.VERSION), codes,
                             {c: LADDER for c in codes})


def build_point(ents):
    lat = sum(e['lat'] for e in ents) / len(ents)
    lon = sum(e['lon'] for e in ents) / len(ents)
    lw, ow, iy, ix = addre_io.parcel_point(lat, lon)
    kw, cr = 2, 14
    keys, recs, names = [], [], []
    for u, later in point_word_keys(TOWN, kw):
        keys.append((u, len(recs), 1))
        slot = len(recs)
        ref = REF_BASE + 4 * slot
        names.append(point_io.name_record(TOWN, LADDER, NLEVEL, point_io.SETTLEMENT, lat=lw, lon=ow,
                                          x=ix, y=iy,
                                          refs=((2, 0x40, ref), (3, 0x40, ref + 1))))
        recs.append(point_io.data_record(LADDER, NLEVEL, slot, point_io.SETTLEMENT, lat=lw, lon=ow,
                                         x=ix, y=iy,
                                         flags=0x4000 if later else 0))
    df = bytearray(frames.POINT_DF)      # +0x112 and +0x130 are the telephone maxima; no telephones
    out, nb, tb = point_io.assemble(frames.point_prefix(iceland_card.BOUNDS, iceland_card.VERSION), bytes(df), keys, recs, names, [], cr, kw,
                                    AREA, NLEVEL, maxkey_hint=frames.point_d(names))
    # the City list: a municipality row and a settlement row, as every factory ADM frame
    adm_names = [(TOWN, LADDER[:2], LANG_OTHER), (TOWN, LADDER, LANG_OTHER)]
    adm_keys = {}
    for i, (nm, lv, lang) in enumerate(adm_names):
        for u, later in point_word_keys(nm, frames.ADM_KW):
            adm_keys.setdefault(u, []).append((i, later))
    at = frames.up(len(out))
    frame, h, dfo = frames.adm(at, AREA, NLEVEL, adm_names, adm_keys)
    out = bytearray(out) + b'\0' * (at - len(out)) + frame
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
    return bytes(out)


def coord(rows, slot=15):
    """COORD001: [u32 size hw][u16 rows][u16 9][u16 0], 18-byte rows (an 80-bit ordinal
    mask, bit 79 - ordinal, then lat min, lon min, lat max, lon max words), and a 6-byte
    trailer that repeats the size. Layout read off both factory files."""
    body = b''.join(m.to_bytes(10, 'big') + struct.pack('>4H', *box) for m, box in rows)
    size = (10 + len(body) + 6) // 2
    t = struct.pack('>IHHH', size, len(rows), 9, 0) + body + struct.pack('>HHH', 0, size, 0)
    blk = frames.up(len(t))
    return refer_header.header({slot: (addre_io.addr(0x1000), blk // 256)},
                               iceland_card.BOUNDS, iceland_card.VERSION) + t + b'\0' * (blk - len(t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--shapes', nargs='+', required=True, help='shape index files from rd_build.py --shape-index')
    ap.add_argument('--area', help='every address of these named areas instead of the POC (comma-separated)')
    ap.add_argument('--only', help='area: keep only these streets (comma-separated, upper case)')
    ap.add_argument('--house-ranges', help='comma-separated street names (upper case), or ALL, to write as '
                                            'per-link address ranges; every other street is unaffected')
    ap.add_argument('--no-ranges-town', default='', help='with --house-ranges ALL: towns (upper case, comma-separated) '
                                                          'that keep the generic street row')
    ap.add_argument('--addresses', help='Stadfangaskra.csv input')
    ap.add_argument('--postcodes', help='postnumeraskra_2024.csv input')
    a = ap.parse_args()
    if a.area:
        a.areas, a.municipalities = parse_areas(a.area)
        return main_area(a)
    if not STREETS:
        raise SystemExit('without --area this builds the test streets in [addr] poc_streets, and there are none')
    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    records = [R for p in a.shapes for R in json.load(open(p))['records']]
    ents = streets()
    for e in ents:
        e['link'] = street_link(records, e)
    addre, keys = build_addre(ents)
    (od / 'ADDRE001.DAT').write_bytes(addre)
    (od / 'ZCODE001.DAT').write_bytes(build_zcode())
    (od / 'POINT001.DAT').write_bytes(build_point(ents))
    (od / 'INTER001.DAT').write_bytes(b'')
    box = (boxword_lat(ICELAND[0]), boxword_lon(ICELAND[1]), boxword_lat(ICELAND[2]), boxword_lon(ICELAND[3]))
    (od / 'REFER001_COORD001.DAT').write_bytes(coord([(1 << 79, box)]))
    (od / 'REFER002_COORD001.DAT').write_bytes(coord([]))
    for e in ents:
        L = e['link']
        print('  %-18s %-18s %04x %04x refs %s  shape @%d link %d (way %d, %d points, %.1f m from the anchor)  items %s'
              % (e['name'], fold(e['name']), L['la'], L['lo'], ' '.join('(%d,0x%02x,%d)' % r for r in L['refs']),
                 L['shape_at'], L['link'], L['way'], L['points'], L['anchor_m'], house_items(e['nums'])))
    (od / 'addr_records.json').write_text(json.dumps(
        [dict(e['link'], items=house_items(e['nums']).hex()) for e in ents], ensure_ascii=False, indent=1))
    print('ADDRE001 %d B, keys %s' % (len(addre), [k[0] for k in keys]))



AREA_MUNICIPALITIES = _cfg['addr']['area_municipalities']  # svfnr -> town, per named area
NAME_REACH = 1500.0       # metres from the address centroid to a road of the street's name
ACCESS_REACH = 500.0      # metres to any road, for a street with no road of its own name


def parse_areas(value):
    names = value.split(',')
    unknown = [n for n in names if n not in AREA_MUNICIPALITIES]
    if unknown:
        raise SystemExit('unknown --area: %s' % ', '.join(unknown))
    municipalities = {}
    for name in names:
        municipalities.update(AREA_MUNICIPALITIES[name])
    return names, municipalities


def ladder(town):
    return (AREA, TOWNS.index(town), 0)


def settlement_ladder(town):
    # ADDRE and ZCODE number settlements from 1 (ICODE leaf + 1; 0 means none), POINT/ADM from 0
    return (AREA, TOWNS.index(town), 1)


def groups(address_path=None, postcode_path=None, municipalities=None):
    """(street, town) -> addresses, the town upper case as ICODE spells it."""
    tn = towns(postcode_path)
    g = collections.defaultdict(list)
    for r in addresses(address_path, municipalities=set(municipalities)):
        g[(r['street'], tn[r['post']].upper())].append(r)
    return g


def capital_entries(g):
    out = []
    for (street, town), rs in g.items():
        nums = sorted({int(r['num']) for r in rs if r['num'].isdigit() and int(r['num'])})
        low = [r for r in rs if nums and r['num'] == str(nums[0])] or rs
        out.append(dict(street=street, name=street.upper(), town=town, nums=nums, addresses=rs,
                        posts=sorted({r['post'] for r in rs}),
                        n=len(rs), lat=sum(r['lat'] for r in rs) / len(rs), lon=sum(r['lon'] for r in rs) / len(rs),
                        anchor=(sum(r['lon'] for r in rs) / len(rs), sum(r['lat'] for r in rs) / len(rs)),
                        low=(low[0]['lon'], low[0]['lat'])))
    return sorted(out, key=lambda e: (fold(e['name']), TOWNS.index(e['town'])))


class Links:
    """Every link of every level-1 drawn shape, by street name and by 0.01-degree cell."""

    def __init__(self, records):
        self.byname = collections.defaultdict(list)
        self.grid = collections.defaultdict(list)
        for R in records:
            for S in R['shapes']:
                pts = S['points']
                nm = shape_index.label_name(S['label']).upper() if S['label'] else ''
                for L in S['links']:
                    js = [j for j, P in enumerate(pts) if P['link'] == L['id']]
                    j0 = js[0] if pts[js[0]]['role'] == 'start' else js[0] - 1
                    item = (R, S, L, pts[j0:js[-1] + 1])
                    if nm: self.byname[nm].append(item)
                    for P in item[3]:
                        self.grid[(int(P['lon'] * 100), int(P['lat'] * 100))].append(item)

    def near(self, p, reach):
        r = int(math.ceil(reach / 400)) + 1
        cx, cy = int(p[0] * 100), int(p[1] * 100)
        seen = {}
        for dx in range(-2 * r, 2 * r + 1):
            for dy in range(-r, r + 1):
                for it in self.grid.get((cx + dx, cy + dy), ()):
                    seen[id(it)] = it
        return list(seen.values())


def run_dist(p, run):
    return min(seg_dist(p, (a['lon'], a['lat']), (b['lon'], b['lat'])) for a, b in zip(run, run[1:]))


def project(p, run):
    best = None
    before = 0.0
    for a, b in zip(run, run[1:]):
        kx = 111320 * math.cos(math.radians(p[1])); ky = 111320
        ax, ay = (a['lon'] - p[0]) * kx, (a['lat'] - p[1]) * ky
        dx, dy = (b['lon'] - a['lon']) * kx, (b['lat'] - a['lat']) * ky
        size = dx * dx + dy * dy
        t = 0 if size == 0 else max(0, min(1, -(ax * dx + ay * dy) / size))
        dist = math.hypot(ax + t * dx, ay + t * dy)
        cross = dx * -ay - dy * -ax
        along = before + t * math.sqrt(size)
        candidate = (dist, along, 1 if cross >= 0 else -1)
        if best is None or candidate[0] < best[0]:
            best = candidate
        before += math.sqrt(size)
    return best


def address_ranges(links, e):
    cands = links.byname.get(e['name'], ())
    if not cands:
        return [], list(e['addresses'])
    matched = []
    for a in e['addresses']:
        p = (a['lon'], a['lat'])
        choices = [(project(p, c[3]), c) for c in cands]
        (dist, along, side), c = min(choices, key=lambda x: x[0][0])
        matched.append((c, side, along, dist, a))
    grouped = collections.defaultdict(list)
    for c, side, along, dist, a in matched:
        if a['num'].isdigit() and int(a['num']):
            R, S, L, run = c
            grouped[(R['code'][0], R['code'][1], S['at'], L['id'], side)].append((along, dist, int(a['num']), a, c))
    out = []
    for key, rows in grouped.items():
        R, S, L, run = rows[0][4]
        nums = [r[2] for r in rows]
        mean_n = sum(nums) / len(nums); mean_a = sum(r[0] for r in rows) / len(rows)
        reverse = sum((r[2] - mean_n) * (r[0] - mean_a) for r in rows) < 0
        first, last = (run[-1], run[0]) if reverse else (run[0], run[-1])
        k, i = first['word'] - R['unit'], last['word'] - R['unit']
        side = -key[-1] if reverse else key[-1]
        flag = 0x54 if side > 0 else 0x58
        refs = addre_io.street_refs(k, i, first['role'] != 'vertex', last['role'] != 'vertex')
        out.append(dict(lo=min(nums), hi=max(nums), side='left' if side > 0 else 'right', side_flag=flag,
                        la=R['code'][0], lo_word=R['code'][1], refs=refs, shape_at=S['at'], link=L['id'],
                        way=L['way'], label=S['label'], addresses=len(rows), max_metres=round(max(r[1] for r in rows), 1),
                        points=[dict(num=r[2], lat=r[3]['lat'], lon=r[3]['lon'], along=r[0]) for r in rows]))
    return sorted(out, key=lambda r: (r['la'], r['lo_word'], r['shape_at'], r['link'], r['side_flag'], r['lo'])), []


def pick(links, e):
    """(how, distance, (R, S, L, run)) for one entry, or None."""
    cands = [(run_dist(e['anchor'], c[3]), c) for c in links.byname.get(e['name'], ())]
    cands = [c for c in cands if c[0] <= NAME_REACH]
    if cands:
        d, c = min(cands, key=lambda c: c[0])
        return 'name', d, c
    cands = [(run_dist(e['anchor'], c[3]), c) for c in links.near(e['anchor'], ACCESS_REACH)]
    cands = [c for c in cands if c[0] <= ACCESS_REACH]
    if cands:
        d, c = min(cands, key=lambda c: c[0])
        return 'access', d, c
    return None


def link_refs(e, how, d, c):
    R, S, L, run = c
    first, last = run[0], run[-1]
    if metres(e['low'], (last['lon'], last['lat'])) < metres(e['low'], (first['lon'], first['lat'])):
        first, last = last, first
    k, i = first['word'] - R['unit'], last['word'] - R['unit']
    refs = addre_io.street_refs(k, i, first['role'] != 'vertex', last['role'] != 'vertex')
    mid = ((run[0]['lat'] + run[1]['lat']) / 2, (run[0]['lon'] + run[1]['lon']) / 2)
    assert addre_io.parcel_words(*mid) == tuple(R['code']), (e['street'], R['code'])
    home = addre_io.parcel_words(e['lat'], e['lon'])
    return dict(street=e['name'], town=e['town'], la=R['code'][0], lo=R['code'][1], refs=refs, shape_at=S['at'],
                link=L['id'], way=L['way'], label=S['label'], how=how, metres=round(d, 1),
                other_parcel=tuple(home) != tuple(R['code']))


def build_addre_capital(ents):
    """Entries with e['ranges'] get per-link rows; every other entry keeps the single
    generic-row form, so no ranged entries reproduces the pre-range output exactly."""
    kw = 1
    cr = 8 + kw + 3
    if any('ranges' in e for e in ents):
        return build_addre_mixed(ents, kw, cr)
    names = [addre_io.name_record(e['name'], e['link']['la'], e['link']['lo'], settlement_ladder(e['town']),
                                  house_items(e['nums']), refs=e['link']['refs']) for e in ents]
    bykey = collections.defaultdict(list)
    for i, e in enumerate(ents):
        for u, later in word_keys(e['name'], kw):
            bykey[u].append((i, 0x4000 if later else 0))
    keys, srecs = [], []
    for u in sorted(bykey):
        hits = sorted(bykey[u])
        keys.append((u, len(srecs), len(hits)))
        for i, fl in hits:
            srecs.append(addre_io.street_record(fl, i, settlement_ladder(ents[i]['town'])))
    srecs, pre = ladder_first(srecs)
    d, w = frames.addre_hints(names)
    out, blocks = addre_io.assemble(frames.addre_prefix(iceland_card.BOUNDS, iceland_card.VERSION), keys, srecs,
                                    names, frames.ADDRE_DF, pre, cr, kw, AREA, NLEVEL - 2,
                                    maxkey_hint=d, namewidth_hint=w)
    return out, keys, blocks


def build_addre_mixed(ents, kw, cr):
    """Same trie and record order as build_addre_capital; ranged entries get one row per
    range (continuation flag 0x8000 past the first), and the rest get a generic row."""
    names, idx_of, range_idx_of = [], {}, {}
    for i, e in enumerate(ents):
        if 'ranges' in e:
            idxs = []
            for j, r in enumerate(e['ranges']):
                idxs.append(len(names))
                names.append(addre_io.name_record(e['name'] if j == 0 else '', r['la'], r['lo_word'],
                                                  settlement_ladder(e['town']) if j == 0 else (),
                                                  addre_io.house_item(r['lo'], r['hi'], r['side_flag']),
                                                  refs=r['refs'], flags=0x18 if j == 0 else 0x19))
            range_idx_of[i] = idxs
        else:
            idx_of[i] = len(names)
            names.append(addre_io.name_record(e['name'], e['link']['la'], e['link']['lo'], settlement_ladder(e['town']),
                                              house_items(e['nums']), refs=e['link']['refs']))
    bykey = collections.defaultdict(list)
    for i, e in enumerate(ents):
        for u, later in word_keys(e['name'], kw):
            bykey[u].append((i, later))
    keys, srecs = [], []
    for u in sorted(bykey):
        start = len(srecs)
        for i, later in sorted(bykey[u], key=lambda h: h[0]):
            e = ents[i]
            if i in range_idx_of:
                for j, name_idx in enumerate(range_idx_of[i]):
                    r = e['ranges'][j]
                    flags = (0x4000 if later else 0) | (0x8000 if j else 0)
                    srecs.append(addre_io.range_record(flags, r['lo'], r['hi'], name_idx, settlement_ladder(e['town'])))
            else:
                srecs.append(addre_io.street_record(0x4000 if later else 0, idx_of[i], settlement_ladder(e['town'])))
        keys.append((u, start, len(srecs) - start))
    srecs, pre = ladder_first(srecs)
    d, w = frames.addre_hints(names)
    out, blocks = addre_io.assemble(frames.addre_prefix(iceland_card.BOUNDS, iceland_card.VERSION), keys, srecs,
                                    names, frames.ADDRE_DF, pre, cr, kw, AREA, NLEVEL - 2,
                                    maxkey_hint=d, namewidth_hint=w)
    return out, keys, blocks


def build_zcode_capital(ents):
    town_of = {}
    for e in ents:
        for p in e['posts']:
            assert town_of.setdefault(str(p), e['town']) == e['town'], (p, e['town'])
    codes = sorted(town_of)
    return zcode_io.assemble(frames.zcode_prefix(AREA, iceland_card.BOUNDS, iceland_card.VERSION), codes,
                             {c: settlement_ladder(town_of[c]) for c in codes}), town_of


def build_point_capital(ents):
    """One settlement per town at its largest postcode's address centroid, and the ADM
    frame with a municipality row and a settlement row per town."""
    by = collections.defaultdict(list)
    for e in ents: by[e['town']].append(e)
    tlist = sorted(by, key=TOWNS.index)
    kw, cr = 2, 14
    keyhits = collections.defaultdict(list)
    for t in tlist:
        for u, later in point_word_keys(t, kw): keyhits[u].append((t, later))
    names, recs, keys = [], [], []
    slot_of = {}
    for t in tlist:
        post_rows = collections.defaultdict(list)
        for e in by[t]:
            for r in e['addresses']:
                post_rows[r['post']].append(r)
        _post, rs = min(post_rows.items(), key=lambda item: (-len(item[1]), item[0]))
        lat = sum(r['lat'] for r in rs) / len(rs); lon = sum(r['lon'] for r in rs) / len(rs)
        lw, ow, iy, ix = addre_io.parcel_point(lat, lon)
        slot_of[t] = len(names)
        ref = REF_BASE + 4 * len(names)
        names.append(point_io.name_record(t, ladder(t), NLEVEL, point_io.SETTLEMENT, lat=lw, lon=ow,
                                          x=ix, y=iy, refs=((2, 0x40, ref), (3, 0x40, ref + 1))))
        slot_of[t] = (len(names) - 1, lw, ow, iy, ix)
    for u in sorted(keyhits):
        hits = sorted(keyhits[u], key=lambda h: (h[1], TOWNS.index(h[0])))
        keys.append((u, len(recs), len(hits)))
        for t, later in hits:
            i, lw, ow, iy, ix = slot_of[t]
            recs.append(point_io.data_record(ladder(t), NLEVEL, i, point_io.SETTLEMENT, lat=lw, lon=ow,
                                             x=ix, y=iy, flags=0x4000 if later else 0))
    out, nb, tb = point_io.assemble(frames.point_prefix(iceland_card.BOUNDS, iceland_card.VERSION), bytes(frames.POINT_DF),
                                    keys, recs, names, [], cr, kw, AREA, NLEVEL, maxkey_hint=frames.point_d(names))
    adm_names = []
    for t in tlist:
        adm_names += [(t, ladder(t)[:2], LANG_OTHER), (t, ladder(t), LANG_OTHER)]
    adm_keys = collections.defaultdict(list)
    for i, (nm, lv, lang) in enumerate(adm_names):
        for u, later in point_word_keys(nm, frames.ADM_KW):
            adm_keys[u].append((i, later))
    at = frames.up(len(out))
    frame, h, dfo = frames.adm(at, AREA, NLEVEL, adm_names, adm_keys)
    out = bytearray(out) + b'\0' * (at - len(out)) + frame
    struct.pack_into('>IH', out, 0x800 + 6 * 280, addre_io.addr(h), 1)
    struct.pack_into('>IH', out, 0x800 + 6 * 287, addre_io.addr(dfo), 1)
    return bytes(out), tlist


def main_area(a):
    od = Path(a.outdir)
    od.mkdir(parents=True, exist_ok=True)
    links = Links([R for p in a.shapes for R in json.load(open(p))['records']])
    plain_towns = set(filter(None, a.no_ranges_town.split(',')))
    all_ranges = a.house_ranges is not None and a.house_ranges.strip().upper() == 'ALL'
    targets = ({s.strip().upper() for s in a.house_ranges.split(',')}
               if a.house_ranges and not all_ranges else None)
    ents, fallback, skipped = [], [], []
    found_targets = set()
    only = set(a.only.split(',')) if a.only else None
    for e in capital_entries(groups(a.addresses, a.postcodes, a.municipalities)):
        if (all_ranges and e['town'] not in plain_towns) or (targets and e['name'] in targets):
            if targets:
                found_targets.add(e['name'])
            e['ranges'], failed = address_ranges(links, e)
            if failed or not e['ranges']:
                del e['ranges']
                if not all_ranges:
                    skipped.append(e)
                    continue
                got = pick(links, e)
                if got is None:
                    skipped.append(e)
                    continue
                e['link'] = link_refs(e, *got)
                fallback.append(e)
        else:
            got = pick(links, e)
            if got is None:
                skipped.append(e); continue
            e['link'] = link_refs(e, *got)
        ents.append(e)
    missing_targets = targets - found_targets if targets else set()
    if missing_targets:
        raise SystemExit('--house-ranges streets not found: %s' % ', '.join(sorted(missing_targets)))
    full = ents
    if only is not None:
        ents = [e for e in full if e['name'] in only]
        print('--only: %d of %d records kept in ADDRE; ZCODE and POINT keep every town and postcode'
              % (len(ents), len(full)))
    addre, keys, blocks = build_addre_capital(ents)
    (od / 'ADDRE001.DAT').write_bytes(addre)
    z, town_of = build_zcode_capital(full)
    (od / 'ZCODE001.DAT').write_bytes(z)
    point, tlist = build_point_capital(full)
    (od / 'POINT001.DAT').write_bytes(point)
    (od / 'INTER001.DAT').write_bytes(b'')
    box = (boxword_lat(ICELAND[0]), boxword_lon(ICELAND[1]),
           boxword_lat(ICELAND[2]), boxword_lon(ICELAND[3]))
    (od / 'REFER001_COORD001.DAT').write_bytes(coord([(1 << 79, box)]))
    (od / 'REFER002_COORD001.DAT').write_bytes(coord([]))
    records = [dict(street=e['name'], town=e['town'], posts=e['posts'], ranges=e['ranges']) if 'ranges' in e else
               dict(e['link'], posts=e['posts'], addresses=e['n'], items=house_items(e['nums']).hex()) for e in ents]
    (od / 'addr_records.json').write_text(json.dumps(records, ensure_ascii=False, indent=1))
    (od / 'addr_skipped.json').write_text(json.dumps(
        [dict(street=e['street'], town=e['town'], addresses=e['n'], lat=e['lat'], lon=e['lon']) for e in skipped],
        ensure_ascii=False, indent=1))
    if all_ranges or targets:
        ranged = [e for e in ents if 'ranges' in e]
        print('range streets %d, ranges %d, addresses %d, fallback streets %d, skipped streets %d (%d addresses)' % (
            len(ranged), sum(len(e['ranges']) for e in ranged), sum(e['n'] for e in ranged),
            len(fallback), len(skipped), sum(e['n'] for e in skipped)))
        print('  side flags: left 0x54, right 0x58 relative to the emitted reference direction')
        print('ADDRE001 %d B, %d keys, %d name blocks' % (len(addre), len(keys), len(blocks)))
        return
    how = collections.Counter(e['link']['how'] for e in ents)
    print('records %d (%d addresses), skipped %d (%d addresses)' % (
        len(ents), sum(e['n'] for e in ents), len(skipped), sum(e['n'] for e in skipped)))
    print('  pointer to a road of its own name: %d, to the nearest road of another name: %d' % (how['name'], how['access']))
    print('  link outside the parcel holding the centroid: %d' % sum(e['link']['other_parcel'] for e in ents))
    print('  hop tags written: 0 (every record covers one link)')
    print('ADDRE001 %d B, %d keys, %d name blocks' % (len(addre), len(keys), len(blocks)))
    print('ZCODE001 %d postcodes: %s' % (len(town_of), ' '.join(sorted(town_of))))
    print('POINT001 towns: %s' % ', '.join('%s (row %d)' % (t, TOWNS.index(t)) for t in tlist))
    per = collections.Counter(e['town'] for e in ents)
    for t in tlist: print('  %-22s %5d records' % (t, per[t]))


if __name__ == '__main__':
    main()
