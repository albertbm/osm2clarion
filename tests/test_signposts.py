#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Section 4 signposts: the record writer against the measured layout, the G words in the
road-shape record, and the offsets that tie the two together.

    python3 -m unittest tests.test_signposts -v

The factory cases need a tile with section 4; set OSM2CLARION_FACTORY_TILE to one (B48R060R.DAT
is the one the spec was measured on) or they skip. Nothing from it is written anywhere.
"""
import json, os, struct, sys, tempfile, unittest, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import clarion_profile, fa_encode, sec4_write, sec4_check, rd_build, osm_roads

TILE_ENV = 'OSM2CLARION_FACTORY_TILE'


def words(b):
    return list(struct.unpack('>%dH' % (len(b) // 2), b))


class TestRecordLayout(unittest.TestCase):
    """The header, the country record and the signpost record."""

    def test_country_only_record_is_the_factory_56_byte_form(self):
        plain, comp = sec4_write.record(2304, 13326, 352)
        want = bytearray(sec4_write.TEMPLATE)
        struct.pack_into('>HH', want, 0, *sec4_write.parcel_code(2304, 13326))
        struct.pack_into('>H', want, 16, 352)
        self.assertEqual(plain, bytes(want))
        self.assertEqual(zlib.decompress(comp), plain)

    def test_one_junction_record_matches_section_6(self):
        text = '0=REYKJAVÍK\x1fHAFNARFJÖRÐUR;2=41'
        subs = [dict(key=0x1b6, groups=[dict(hdr=0x14, entries=[dict(dir=0x2800, pairs=[(0x0140, 0x253)], text=text)])])]
        plain = sec4_write.encode(dict(code=(0xa40c, 0xd03e), country=352, subs=subs))
        w = words(plain)
        tb = text.encode('utf-8') + b'\0' * (len(text.encode('utf-8')) % 2)
        self.assertEqual(w[:11], [0xa40c, 0xd03e, 9, 37 - 9, 0xffff, 0, 37, len(tb) // 2, 352, 0, 0])
        self.assertEqual(w[11:37], [0xffff, 26, 0xffff, 0, 0xffff, 0, 0x10, 1, 0xffff, 0, 0xffff, 0, 0, 0, 0, 0,
                                    0x1401, 0, 0x4000 | len(tb), 3, 0, 0x2800, 0x0140, 0x253, 0, 0x1b6])
        self.assertEqual(plain[74:], tb)
        self.assertEqual(sec4_write.sub_offsets(subs), [11])

    def test_texts_are_shared_and_sub_records_chained(self):
        e = lambda d, ref, t: dict(dir=d, pairs=[(0x0140, ref)], text=t)
        subs = [dict(key=10, groups=[dict(hdr=0x14, entries=[e(0x2800, 5, '0=A')])]),
                dict(key=20, groups=[dict(hdr=0x18, entries=[e(0x1800, 5, '0=BB'), e(0x2400, 7, '0=A')])])]
        plain = sec4_write.encode(dict(code=(1, 2), country=352, subs=subs))
        w = words(plain)
        self.assertEqual(sec4_write.sub_offsets(subs), [11, 37])
        self.assertEqual(w[11], 37, 'the first link word names the second sub-record')
        self.assertEqual(w[37], 0xffff)
        self.assertEqual(w[38], 26 + 7)
        self.assertEqual(w[6], 37 + 33)
        self.assertEqual(plain[2 * w[6]:], b'0=A\0' + b'0=BB')
        d = sec4_write.decode(plain)
        self.assertEqual([s['key'] for s in d['subs']], [10, 20])
        self.assertEqual([x['text'] for x in d['subs'][1]['groups'][0]['entries']], [b'0=BB', b'0=A\0'])
        self.assertEqual(sec4_write.encode(d), plain)

    def test_sign_text(self):
        f = sec4_write.sign_text
        self.assertEqual(f({'destination': 'Hvalfjörður;Akureyri;Akranes', 'destination:ref': '47'}, upper=True),
                         '0=HVALFJÖRÐUR\x1fAKUREYRI\x1fAKRANES;2=47')
        self.assertEqual(f({'destination:ref': '1', 'destination:ref:to': '54'}), '2=1\x1f(54)')
        self.assertEqual(f({'junction:ref': '23', 'destination': 'Vík'}), '0=Vík;4=23')
        self.assertEqual(f({'destination': 'a;b;c;d;e;f;g;h'}), '0=a\x1fb\x1fc\x1fd\x1fe\x1ff', 'six names at most')
        self.assertIsNone(f({'destination': ' ; '}))

    def test_load_signs_drops_a_null_text_entry(self):
        # an older sidecar can still hold a blank sign written before rd_build skipped them
        side = dict(cell='x', country=352, parcels=[dict(y=0, x=0, row=0, col=0, subs=[
            dict(key=1, groups=[dict(hdr=sec4_write.GROUP_HDR,
                                      entries=[dict(dir=0x2800, pairs=[[0x0140, 5]], text=None)])])])])
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(side, f); path = f.name
        try:
            _, out = sec4_write.load_signs(path)
            self.assertEqual(out[(0, 0)][(0, 0)][0]['groups'][0]['entries'], [])
        finally:
            os.unlink(path)


class TestInheritDest(unittest.TestCase):
    """osm_roads.inherit_dest: a feeder joins its dest-tagged link at the right travel end."""

    def way(self, id, nodes, dir=0, dest=None):
        w = dict(id=id, hw='motorway_link', dir=dir, nodes=nodes)
        if dest: w['dest'] = dest
        return w

    def test_oneway_minus_one_looks_up_its_travel_start_not_nodes_zero(self):
        # tagged travels (20,0) -> (10,0) since dir=-1; feeder arrives at (20,0), its travel start
        tagged = self.way(1, [(10, 0), (20, 0)], dir=-1, dest={'destination': 'Akureyri'})
        feeder = self.way(2, [(30, 0), (20, 0)], dir=1)
        ways = [tagged, feeder]
        self.assertEqual(osm_roads.inherit_dest(ways), 1)
        self.assertEqual(feeder['dest'], tagged['dest'])


class TestInteriorNodeMerge(unittest.TestCase):
    """rd_build.build_graphs splits a road at a ramp's merge node on its own node-sharing
    count, even when OSM never split the road's own way there. So find_signposts, which
    runs after that split, does not need the ramp-names fix for unsplit roads (c6c61f9)."""

    def setUp(self):
        rd_build.use_profile(clarion_profile.load(str(ROOT / 'profiles' / 'iceland.toml')))

    def test_road_splits_at_the_ramps_interior_node(self):
        road = dict(id=1, hw='trunk', name='MAIN', ref=None, dir=0,
                    nodes=[(-21.902, 64.130), (-21.900, 64.130), (-21.898, 64.130)])
        ramp = dict(id=2, hw='trunk_link', ref=None, dir=1, dest={'destination': 'Test'},
                    nodes=[(-21.900, 64.130), (-21.900, 64.132)])
        parcels = rd_build.build_graphs([road, ramp], 'B60R080R', 1, True)
        P = next(p for p in parcels.values() if not p['ring'])
        road_links = [l for l in P['links'] if l[3] == 1]
        self.assertEqual(len(road_links), 2, 'one OSM way, unsplit, becomes two links either side of the merge')
        groups = rd_build.link_groups(P['links'], True)
        found = rd_build.find_signposts(P['links'], list(P['order']), P['index'], groups,
                                        {2: ramp['dest']}, upper=True)
        self.assertEqual(len(found), 1, 'the ramp is still signed off the road that OSM left unsplit')


class TestFactoryRoundTrip(unittest.TestCase):
    """Decode the factory's own records and write them back byte for byte."""

    @classmethod
    def setUpClass(cls):
        path = os.environ.get(TILE_ENV)
        if not path or not os.path.exists(path):
            raise unittest.SkipTest('set %s to a factory tile with section 4 (B48R060R.DAT)' % TILE_ENV)
        from tile_scan import TileScan
        cls.recs = {}
        with TileScan(path) as t:
            for r in t.records(section=4):
                if r.n > 65536 or not r.is_zlib: continue
                try: p = r.plain()
                except zlib.error: continue
                if len(p) >= 24 and struct.unpack_from('>H', p, 4)[0] == 9:
                    cls.recs[struct.unpack_from('>HH', p, 0)] = p

    def test_the_spec_examples(self):
        for code, nsubs, texts in (((0xa40c, 0xd03e), 3, [b'0=COURTERON;2=D971', b'0=(A5)\x1f(A26)']),
                                   ((0xa41d, 0xd06c), 3, [b'0=CHAUMONT\x1fNANCY\x1fMULHOUSE\x1fDIJON-LYON;2=A5']),
                                   ((0xa50d, 0xd18c), None, [])):
            if code not in self.recs: self.skipTest('%04x %04x is not in this tile' % code)
            p = self.recs[code]
            d = sec4_write.decode(p)
            if nsubs: self.assertEqual(len(d['subs']), nsubs)
            got = [e['text'].rstrip(b'\0') for s in d['subs'] for g in s.get('groups', ()) for e in g['entries']]
            for t in texts: self.assertIn(t, got)
            self.assertEqual(sec4_write.encode(d), p, '%04x %04x' % code)

    def test_every_record(self):
        bad = [code for code, p in self.recs.items() if sec4_write.encode(sec4_write.decode(p)) != p]
        self.assertGreater(len(self.recs), 1000)
        self.assertEqual(bad, [])

    def test_sub_record_sizes_follow_the_layout(self):
        for code, p in list(self.recs.items())[:500]:
            w = words(p); d = sec4_write.decode(p)
            self.assertEqual(sec4_write.sub_offsets(d['subs']), [L for L in walk(w)], '%04x %04x' % code)


def walk(w):
    L = 11
    while True:
        yield L
        if w[L] == 0xffff: return
        L = w[L]


class TestGuidanceWords(unittest.TestCase):
    """G in the 0x00fa record: shape_words places it, the offsets follow it."""

    NODES = [(100, 200), (900, 240), (1500, 1800), (900, 1000)]
    CHAIN = [(1, 0, 1, [(300, 210), (600, 225)]), (2, 1, 2, [(1100, 900)])]

    def test_shape_words_with_g(self):
        plain = fa_encode.shape_words(self.CHAIN, self.NODES, styles=[0x101e, 0x101e])
        withg = fa_encode.shape_words(self.CHAIN, self.NODES, styles=[0x101e, 0x101e], gs={1: 37, 2: 63})
        self.assertEqual(len(withg), len(plain) + 2)
        self.assertEqual(withg[0], plain[0] + 2, 'the length word counts the two G words')
        self.assertEqual(withg[1], plain[1], 'the point count does not')
        self.assertEqual(withg[:7], [withg[0]] + plain[1:7])
        # occurrence 1 = the end of link 1, where link 2's header starts: y gets 0x1000, G after the key
        p = 7 + 2 + 3 + 2 * 3                     # start point, link-1 header, three points
        self.assertEqual(plain[p - 1] & 0x1000, 0)
        self.assertEqual(withg[p - 1] & 0x1000, 0x1000)
        self.assertEqual(withg[p:p + 3], [0xffff, 37, 2], 'key, G, link id (no fresh style word)')
        self.assertEqual(withg[-3] & 0x1000, 0x1000, "the last point's y announces a G in the tail")
        self.assertEqual(withg[-2:], [0xffff, 63], 'tail: key then G')
        self.assertEqual(plain[-1], 0xffff)

    def test_shape_words_with_upper_link(self):
        plain = fa_encode.shape_words(self.CHAIN, self.NODES, styles=[0x101e, 0x101e])
        up = fa_encode.shape_words(self.CHAIN, self.NODES, styles=[0x101e, 0x101e], upper=[0x2007])
        self.assertEqual(up[0], 0x2000 | plain[0] + 1, 'count in bits 13-15, length one word more')
        self.assertEqual(up[1:6], plain[1:6])
        self.assertEqual(up[6:8], [0x2007, 0x0000], 'before the last header word')
        self.assertEqual(up[8:], plain[7:])
        pts = fa_encode.point_words(self.CHAIN, 1, [0x101e, 0x101e], head=1)
        self.assertEqual([o for o, _, _ in pts], [o + 1 for o, _, _ in fa_encode.point_words(self.CHAIN, 1, [0x101e, 0x101e])])
        for slot, xoff, nid in fa_encode.key_slots(self.CHAIN, [0x101e, 0x101e], head=1):
            self.assertEqual(up[xoff] & 0x7ff, self.NODES[nid][0])
            self.assertEqual(up[slot], 0xffff)
        rec, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN, [(3, 2, 3, [])]], hws=['primary', 'primary'],
                                        styles=[[0x101e, 0x101e], [0x101e]], country=352, uppers=[[0x2007], []])
        strings = fa_encode.link_strings(rec)
        self.assertEqual([s['typ'] for s in strings], [2, 0])
        self.assertEqual([[p[1] for p in s['pts']] for s in strings], [[100, 300, 600, 900, 1100, 1500], [1500, 900]])
        shapes = fa_encode.parse_record(rec, 1)[2]
        self.assertTrue(all(S['ok'] for S in shapes))
        self.assertEqual([S['links'] for S in shapes], [[1, 2], [3]])

    def test_link_groups_break_where_the_upper_link_changes(self):
        link = lambda a, b: (a, b, 10.0, 1, None, 'primary', None, None, [], 0, 0, None)
        L = [link(0, 1), link(1, 2), link(2, 3)]
        self.assertEqual(rd_build.link_groups(L, True), [[0, 1, 2]])
        self.assertEqual(rd_build.link_groups(L, True, [7, 7, 8]), [[0, 1], [2]])
        self.assertEqual(rd_build.link_groups(L, True, [None, None, None]), [[0, 1, 2]])

    def test_way_line_span(self):
        line = rd_build.WayLine([(0.0, 64.0), (0.001, 64.0), (0.001, 64.001), (0.0, 64.0)])
        off, lo, hi = line.span((0.0005, 64.0), (0.001, 64.0005), 80)
        self.assertLess(off, 0.01)
        self.assertAlmostEqual(lo, rd_build.dist((0.0, 64.0), (0.0005, 64.0)), 3)
        self.assertGreater(hi, lo)
        total = line.cum[-1]
        _, lo, hi = line.span((0.001, 64.001), (0.0, 64.0), total - line.cum[2])
        self.assertEqual((lo, hi), (line.cum[2], total), 'the closing node counts as the end, not the start')

    def test_point_and_key_slots_shift_with_g(self):
        gs = {0: 11, 1: 37, 2: 63}
        pts = fa_encode.point_words(self.CHAIN, 1, [0x101e, 0x101e], gs)
        slots = fa_encode.key_slots(self.CHAIN, [0x101e, 0x101e], gs)
        sw = fa_encode.shape_words(self.CHAIN, self.NODES, styles=[0x101e, 0x101e], gs=gs)
        for off, lid, role in pts:
            self.assertEqual(sw[off] & 0x7ff, [x for x, y in self.coords()][[o for o, _, _ in pts].index(off)])
        for slot, xoff, nid in slots:
            self.assertEqual(sw[xoff] & 0x7ff, self.NODES[nid][0], 'x word of occurrence')
            self.assertEqual(sw[slot], 0xffff, 'the key placeholder')
            self.assertIn(sw[slot + 1], gs.values(), 'G follows the key')
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'],
                                          styles=[[0x101e, 0x101e]], country=352, gs={0: gs})
        self.assertEqual(fa_encode.link_strings(plain)[0]['pts'][0][5]['G'], 11)

    def coords(self):
        out = [self.NODES[self.CHAIN[0][1]]]
        for lid, a, b, inner in self.CHAIN: out += inner + [self.NODES[b]]
        return out

    def test_link_strings_reads_back_what_build_record_wrote(self):
        gs = {0: {1: 37}}
        plain, _ = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'],
                                          styles=[[0x101e, 0x101e]], country=352, gs=gs)
        S = fa_encode.link_strings(plain)
        self.assertEqual(len(S), 1)
        nodes = [(pt[0], pt[1], pt[2], pt[5]) for pt in S[0]['pts'] if pt[5] is not None]
        self.assertEqual([(x, y) for _, x, y, _ in nodes], [self.NODES[0], self.NODES[1], self.NODES[2]])
        self.assertEqual([n['G'] for _, _, _, n in nodes], [None, 37, None])
        occ = fa_encode.occurrence_words(fa_encode.build_record.last_index)
        self.assertEqual(occ[0], [o for o, _, _, _ in nodes], 'occurrence_words names the x words')
        self.assertEqual(sec4_check.key_base(plain), 22)

    def test_without_gs_nothing_changes(self):
        a = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'],
                                   styles=[[0x101e, 0x101e]], country=352)
        b = fa_encode.build_record(1, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'],
                                   styles=[[0x101e, 0x101e]], country=352, gs={})
        self.assertEqual(a, b)
        c = fa_encode.build_record(2, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'], country=352, gs={0: {1: 5}})
        d = fa_encode.build_record(2, 96, 898, 7, 9, self.NODES, [self.CHAIN], hws=['primary'], country=352)
        self.assertEqual(c, d, 'level 2 ignores gs')


class TestParcelInvariants(unittest.TestCase):
    """A synthetic parcel: a two-way road through a junction, a one-way ramp leaving it, a
    crossing road, built the way rd_build does it, then checked the way sec4_check does."""

    def parcel(self):
        # nodes: 0 road start, 1 junction, 2 road end, 3 ramp end, 4 crossing road end, 5 the other
        nodes = [(100, 1000), (1000, 1000), (1900, 1000), (1700, 1300), (1000, 100), (1000, 1900)]
        idx = {n: i for i, n in enumerate(nodes)}
        # (from, to, length, way, name, hw, ref, maxspeed, inner, chain id, dir)
        L = [(nodes[0], nodes[1], 900, 10, 'VESTURLANDSVEGUR', 'trunk', '1', None, [(500, 1000)], 0, 0),
             (nodes[1], nodes[2], 900, 10, 'VESTURLANDSVEGUR', 'trunk', '1', None, [], 0, 0),
             (nodes[1], nodes[3], 800, 20, None, 'trunk_link', None, None, [(1300, 1100)], 1, 1),
             (nodes[4], nodes[1], 900, 30, 'X', 'secondary', None, None, [], 2, 0),
             (nodes[1], nodes[5], 900, 30, 'X', 'secondary', None, None, [], 2, 0)]
        groups = rd_build.link_groups(L, True)
        ids = rd_build.link_ids(L, 1)
        chains = rd_build.chains_of(L, idx, True, ids, groups)
        return nodes, idx, L, groups, chains

    def build(self, signs):
        nodes, idx, L, groups, chains = self.parcel()
        found = rd_build.find_signposts(L, nodes, idx, groups, signs, upper=True)
        styles = [[0x101e] * len(c) for c in chains]; hws = [L[g[0]][5] for g in groups]
        dirs = [[L[k][10] for k in g] for g in groups]
        gs = {}
        for s in found: gs.setdefault(s['exit'][0], {})[s['exit'][1]] = 0
        fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains, hws=hws, styles=styles, country=352, dirs=dirs, gs=gs)
        occ = fa_encode.occurrence_words(fa_encode.build_record.last_index)
        subs = rd_build.signpost_subs(found, occ)
        for s, Lw in zip(subs, sec4_write.sub_offsets(subs)):
            for i, o in gs.items():
                for n in o:
                    if occ[i][n] - 22 == s['key']: o[n] = Lw
        fa, _ = fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains, hws=hws, styles=styles, country=352, dirs=dirs, gs=gs)
        s4 = sec4_write.encode(dict(code=struct.unpack_from('>HH', fa, 0), country=352, subs=subs))
        return found, subs, fa, s4

    def test_the_ramp_gets_one_sign_from_the_west(self):
        found, subs, fa, s4 = self.build({20: {'destination': 'Akranes', 'destination:ref': '47'}})
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertEqual(s['text'], '0=AKRANES;2=47')
        self.assertEqual(s['bits'], sec4_write.APPROACH_FWD, 'the road link runs west to east into the junction')
        self.assertEqual(len(subs), 1)
        e = subs[0]['groups'][0]['entries'][0]
        self.assertEqual(e['dir'], 0x2800)
        self.assertEqual(e['pairs'][0][0], 0x0140)
        S = fa_encode.link_strings(fa)
        nodes = sec4_check.nodes_of(S)
        J = nodes[subs[0]['key'] + 22]; R = nodes[e['pairs'][0][1] + 22]
        self.assertEqual(S[J[0]]['pts'][J[1]][1:3], (1000, 1000), 'the key names the junction')
        self.assertEqual(S[R[0]]['pts'][R[1]][1:3], (100, 1000), 'the ref names the far end of the approach link')
        self.assertEqual(S[J[0]]['pts'][J[1]][5]['G'], 11)
        self.assertEqual(sec4_check.check_pair(fa, s4), [])
        self.assertEqual(sec4_check.check_pair(fa, s4, subs), [])

    def test_two_way_ramp_of_two_links_signs_only_the_entrance(self):
        # Way 20 is split into two links by node M. L1 leaves Grove; L2's far end only
        # reaches Vesturlandsvegur, the road the ramp is signed for, and must stay unsigned.
        nodes = [(-1000, 1000), (0, 1000), (500, 1000), (1000, 1000), (1500, 1000)]
        idx = {n: i for i, n in enumerate(nodes)}
        L = [(nodes[0], nodes[1], 1000, 1, 'Grove', 'secondary', None, None, [], 10, 0),
             (nodes[1], nodes[2], 500, 20, None, 'trunk_link', None, None, [], 11, 0),
             (nodes[2], nodes[3], 500, 20, None, 'trunk_link', None, None, [], 11, 0),
             (nodes[3], nodes[4], 500, 2, 'Vesturlandsvegur', 'trunk', None, None, [], 12, 0)]
        groups = rd_build.link_groups(L, True)
        found = rd_build.find_signposts(L, nodes, idx, groups, {20: {'destination': 'Vesturlandsvegur'}}, upper=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['exit'], (1, 0), 'signed where the ramp leaves Grove')
        self.assertEqual(found[0]['approach'], (0, 0), 'the approach is Grove, not the ramp\'s own destination road')

    def test_rings_survive_the_g_words(self):
        _, _, fa, _ = self.build({20: {'destination': 'Akranes'}})
        S = fa_encode.link_strings(fa)
        nodes = sec4_check.nodes_of(S)
        at_junction = [off for off, (si, k) in nodes.items() if S[si]['pts'][k][1:3] == (1000, 1000)]
        self.assertEqual(len(at_junction), 3, 'the junction appears once per chain through it')
        self.assertEqual(sec4_check.ring_problems(S, nodes, 22), [])
        for off in at_junction:
            si, k = nodes[off]
            self.assertIn(S[si]['pts'][k][5]['key'] + 22, at_junction)

    def test_a_wrong_offset_is_caught(self):
        _, subs, fa, s4 = self.build({20: {'destination': 'Akranes'}})
        broken = dict(code=struct.unpack_from('>HH', fa, 0), country=352,
                      subs=[dict(key=subs[0]['key'] + 1, groups=subs[0]['groups'])])
        probs = sec4_check.check_pair(fa, sec4_write.encode(broken))
        self.assertTrue(any('is not a node record' in p or 'whose G' in p for p in probs), probs)
        w = bytearray(fa)
        S = fa_encode.link_strings(fa)
        off = [pt[0] for s in S for pt in s['pts'] if pt[5] and pt[5]['G'] is not None][0]
        struct.pack_into('>H', w, 2 * (off + 3), 99)   # the G word of a node with a style word
        probs = sec4_check.check_pair(bytes(w), s4)
        self.assertTrue(probs)

    def test_blank_destination_tags_produce_no_sign(self):
        # a destination tag that trims to nothing must never reach a sub-record as a null
        nodes, idx, L, groups, chains = self.parcel()
        found = rd_build.find_signposts(L, nodes, idx, groups, {20: {'destination': ' ; '}}, upper=True)
        self.assertEqual(found, [])

    def test_without_signs_the_record_is_unchanged(self):
        nodes, idx, L, groups, chains = self.parcel()
        styles = [[0x101e] * len(c) for c in chains]; hws = [L[g[0]][5] for g in groups]
        a = fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains, hws=hws, styles=styles, country=352)
        self.assertEqual(rd_build.find_signposts(L, nodes, idx, groups, {}, True), [])
        b = fa_encode.build_record(1, 96, 898, 7, 9, nodes, chains, hws=hws, styles=styles, country=352, gs={})
        self.assertEqual(a, b)
        self.assertEqual(sec4_check.check_pair(a[0], None), [])


if __name__ == '__main__':
    unittest.main()
