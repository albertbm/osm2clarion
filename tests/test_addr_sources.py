#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The osm and ryhti address readers, on synthetic rows."""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))
import addr_sources  # noqa: E402

BOX = (62.1, 62.5, 25.0, 26.0)
HEADER = 'address_key building_key street street_swe number number2 letter letter2 postcode town municipality lat lon'.split()


class TestHousenumber(unittest.TestCase):
    def test_splits(self):
        s = addr_sources.split_housenumber
        self.assertEqual(s('12'), ('12', ''))
        self.assertEqual(s('12a'), ('12', 'A'))
        self.assertEqual(s('12 A'), ('12', 'A'))
        self.assertEqual(s('12-14'), ('12', ''))
        self.assertEqual(s('12b-d'), ('12', 'B'))
        self.assertEqual(s('3 ä'), ('3', 'Ä'))

    def test_no_number(self):
        for v in ('', None, 'A', 'n/a'):
            self.assertIsNone(addr_sources.split_housenumber(v))


class TestOsmRow(unittest.TestCase):
    def test_row(self):
        r = addr_sources._osm_row({'addr:street': 'Kauppakatu', 'addr:housenumber': '12 a',
                                   'addr:postcode': '40100', 'addr:city': 'Jyväskylä'}, 62.24, 25.75, BOX)
        self.assertEqual(r, dict(street='Kauppakatu', dative='', svf='', num='12', letter='A', post=40100,
                                 lat=62.24, lon=25.75, town='Jyväskylä'))

    def test_missing_postcode_is_zero(self):
        r = addr_sources._osm_row({'addr:street': 'X', 'addr:housenumber': '1'}, 62.2, 25.5, BOX)
        self.assertEqual((r['post'], r['town']), (0, ''))

    def test_skips(self):
        t = {'addr:street': 'X', 'addr:housenumber': '1'}
        self.assertIsNone(addr_sources._osm_row({'addr:housenumber': '1'}, 62.2, 25.5, BOX))
        self.assertIsNone(addr_sources._osm_row({'addr:street': 'X', 'addr:housenumber': 'B'}, 62.2, 25.5, BOX))
        self.assertIsNone(addr_sources._osm_row(t, 61.0, 25.5, BOX))


@unittest.skipUnless(subprocess.run(['which', 'osmium'], capture_output=True).returncode == 0,
                     'osmium command line tool not installed')
class TestOsmFile(unittest.TestCase):
    def test_node_and_building_way(self):
        xml = '''<?xml version="1.0"?>
<osm version="0.6">
 <node id="1" lat="62.20" lon="25.50"><tag k="addr:street" v="Testitie"/><tag k="addr:housenumber" v="5b"/><tag k="addr:postcode" v="40100"/></node>
 <node id="2" lat="62.30" lon="25.60"/><node id="3" lat="62.30" lon="25.62"/>
 <node id="4" lat="62.32" lon="25.62"/><node id="5" lat="62.32" lon="25.60"/>
 <way id="10"><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="2"/>
  <tag k="building" v="yes"/><tag k="addr:street" v="Mallikuja"/><tag k="addr:housenumber" v="7"/></way>
 <node id="6" lat="62.40" lon="25.60"><tag k="addr:street" v="Pois"/><tag k="addr:housenumber" v="1"/></node>
</osm>'''
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, 'a.osm'), os.path.join(d, 'a.osm.pbf')
            Path(src).write_text(xml)
            subprocess.run(['osmium', 'cat', src, '-o', out], check=True, capture_output=True)
            rows = list(addr_sources.osm_addresses(out, (62.0, 62.35, 25.0, 26.0)))
        by = {r['street']: r for r in rows}
        self.assertEqual(sorted(by), ['Mallikuja', 'Testitie'])
        self.assertEqual((by['Testitie']['num'], by['Testitie']['letter'], by['Testitie']['post']), ('5', 'B', 40100))
        self.assertAlmostEqual(by['Mallikuja']['lat'], 62.30, places=1)
        self.assertAlmostEqual(by['Mallikuja']['lon'], 25.61, places=2)


class TestRyhti(unittest.TestCase):
    def rows(self, **kw):
        base = [
            ['k1', 'b1', 'Kauppakatu', '', '12', '', 'a', '', '40100', 'JYVÄSKYLÄ', '179', '62.24', '25.75'],
            ['k2', 'b2', 'Isokuja', 'Stora gränden', '3', '5', '', '', '41800', 'KORPILAHTI', '179', '62.00', '25.50'],
            ['k3', 'b3', '', '', '', '', '', '', '40100', 'JYVÄSKYLÄ', '179', '62.24', '25.75'],
            ['k4', 'b4', 'Muurametie', '', '9', '', '', '', '40950', 'MUURAME', '931', '62.13', '25.67'],
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'r.csv')
            with open(p, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(HEADER)
                w.writerows(base)
            return list(addr_sources.ryhti_addresses(p, **kw))

    def test_rows(self):
        r = self.rows(box=BOX)
        self.assertEqual([x['street'] for x in r], ['Kauppakatu', 'Muurametie'])
        self.assertEqual(r[0], dict(street='Kauppakatu', dative='', svf='179', num='12', letter='A', post=40100,
                                    lat=62.24, lon=25.75, town='JYVÄSKYLÄ', street_swe=''))

    def test_box_and_municipalities(self):
        self.assertEqual([x['street'] for x in self.rows(box=(62.2, 62.3, 25.0, 26.0))], ['Kauppakatu'])
        self.assertEqual([x['street'] for x in self.rows(municipalities={'931'})], ['Muurametie'])


class TestDispatch(unittest.TestCase):
    def test_addr_csv_follows_source(self):
        for addr in ({'format': 'ryhti'}, {'source': 'register', 'register': 'ryhti'}):
            with self.subTest(addr=addr):
                self.check(addr)

    def check(self, addr):
        import addr_csv
        with tempfile.TemporaryDirectory() as d:
            csvp = os.path.join(d, 'r.csv')
            with open(csvp, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(HEADER)
                w.writerow(['k1', 'b1', 'Kauppakatu', '', '12', '', 'a', '', '40100', 'JYVÄSKYLÄ', '179', '62.24', '25.75'])
            cfg = os.path.join(d, 'c.json')
            Path(cfg).write_text(json.dumps({'addr': dict(addr, box=list(BOX)),
                                             'data': {'addresses': csvp}}))
            old = os.environ.get('O2C_CONFIG')
            os.environ['O2C_CONFIG'] = cfg
            try:
                self.assertEqual([r['num'] for r in addr_csv.addresses()], ['12'])
                self.assertEqual(addr_csv.towns(), {40100: 'JYVÄSKYLÄ'})
            finally:
                if old is None:
                    del os.environ['O2C_CONFIG']
                else:
                    os.environ['O2C_CONFIG'] = old


class TestSource(unittest.TestCase):
    def test_new_keys(self):
        src = addr_sources.source
        self.assertEqual(src({'source': 'osm'}), ('osm', None))
        self.assertEqual(src({'source': 'register', 'register': 'ryhti'}), ('register', 'ryhti'))
        self.assertEqual(src({'source': 'register', 'register': 'stadfangaskra'}), ('register', 'stadfangaskra'))

    def test_old_format_key_still_reads(self):
        src = addr_sources.source
        self.assertEqual(src({'format': 'osm'}), ('osm', None))
        self.assertEqual(src({'format': 'ryhti'}), ('register', 'ryhti'))
        self.assertEqual(src({'format': 'stadfangaskra'}), ('register', 'stadfangaskra'))
        self.assertEqual(src({}, default='stadfangaskra'), ('register', 'stadfangaskra'))
        self.assertEqual(addr_sources.layout({'source': 'osm'}), 'osm')
        self.assertEqual(addr_sources.layout({'format': 'ryhti'}), 'ryhti')

    def test_refuses_what_it_cannot_read(self):
        src = addr_sources.source
        for cfg in ({}, {'source': 'register'}, {'source': 'register', 'register': 'dvv'}, {'source': 'csv'},
                    {'format': 'dvv'}, {'source': 'osm', 'format': 'osm'}):
            with self.assertRaises(SystemExit, msg=cfg):
                src(cfg)


class TestProfiles(unittest.TestCase):
    def raw(self, name):
        import tomllib
        return tomllib.loads((ROOT / name).read_text(encoding='utf-8'))

    def test_finland_reads_the_ryhti_register(self):
        raw = self.raw('profiles/finland.toml')
        self.assertEqual(addr_sources.source(raw['addr']), ('register', 'ryhti'))
        self.assertTrue(raw['data']['addresses'].startswith('build/'))
        self.assertNotIn('municipality', raw['addr'])
        self.assertEqual(raw['areas']['jyvaskyla']['municipality'], 'JYVÄSKYLÄ')
        self.assertNotIn('municipality', raw['areas']['finland'])
        local = self.raw('examples/finland.local.toml')
        self.assertEqual(addr_sources.source(local['addr']), ('osm', None))

    def test_iceland_reads_as_before(self):
        # same register as the old format = "stadfangaskra", so the same rows
        raw = self.raw('profiles/iceland.toml')
        self.assertEqual(addr_sources.source(raw['addr']), ('register', 'stadfangaskra'))
        self.assertEqual(addr_sources.layout(raw['addr']), 'stadfangaskra')


if __name__ == '__main__':
    unittest.main()
