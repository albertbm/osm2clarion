#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""addr_csv.area_towns: the ICODE town rows follow the areas' addresses, poc_town first."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools' / 'addr'))
import addr_csv  # noqa: E402

HEAD = 'SVFNR,POSTNR,HEITI_NF,HEITI_TGF,HUSNR,BOKST,N_HNIT_WGS84,E_HNIT_WGS84\n'


class AreaTowns(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        rows = ['0000,101,Laugavegur,,1,,64.14,-21.93', '8401,780,Hafnarbraut,,2,,64.25,-15.2',
                '8710,845,Hrunamannavegur,,3,,64.12,-20.32', '9999,600,Glerárgata,,4,,65.68,-18.1',
                '8401,780,,,5,,64.25,-15.2']
        with open(os.path.join(d, 'a.csv'), 'w', encoding='utf-8') as f:
            f.write(HEAD + '\n'.join(rows) + '\n')
        with open(os.path.join(d, 'p.csv'), 'w', encoding='utf-8') as f:
            f.write('101;Reykjavík\n780;Höfn í Hornafirði\n845;Flúðir\n600;Akureyri\n')
        cfg = dict(data=dict(addresses=os.path.join(d, 'a.csv'), postcodes=os.path.join(d, 'p.csv')),
                   addr=dict(poc_town='REYKJAVÍK', alphabet='AÁBDÐEÉFGHIÍJKLMNOÓPRSTUÚVXYÝÞÆÖ',
                             area_municipalities={'capital': {'0000': 'Reykjavík'},
                                                  'south': {'8401': 'Hornafjörður', '8710': 'Hrunamannahreppur'}}))
        with open(os.path.join(d, 'c.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        self.env = os.environ.get('O2C_CONFIG')
        os.environ['O2C_CONFIG'] = os.path.join(d, 'c.json')

    def tearDown(self):
        if self.env is None:
            os.environ.pop('O2C_CONFIG', None)
        else:
            os.environ['O2C_CONFIG'] = self.env
        self.dir.cleanup()

    def test_rows(self):
        # Akureyri's municipality is in no area; Ú sorts after U, so FLÚÐIR before HÖFN
        self.assertEqual(addr_csv.area_towns(), ('REYKJAVÍK', 'FLÚÐIR', 'HÖFN Í HORNAFIRÐI'))


if __name__ == '__main__':
    unittest.main()
