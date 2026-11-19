#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""pull_ryhti: paging, resuming after a stop, retries and one row per address_key, offline."""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))
import pull_ryhti


def feature(key):
    p = dict(address_key=key, building_key='b' + key, address_name_fin='Katu', address_name_swe='Gatan',
             number_part_of_address_number=1, number_part_of_address_number2=None,
             subdivision_letter_of_address_number=None, subdivision_letter_of_address_number2=None,
             postal_code='00100', postal_office_fin='HELSINKI', municipality_number='091')
    return dict(properties=p, geometry=dict(coordinates=[24.9, 60.2]))


class Service:
    """Pages of a live table, `keys` in address_key order; fail_at stops the run at that call."""

    def __init__(self, keys, fail_at=None):
        self.keys, self.fail_at, self.starts = keys, fail_at, []

    def __call__(self, url):
        if len(self.starts) == self.fail_at:
            raise KeyboardInterrupt
        start = int(url.split('startIndex=')[1].split('&')[0])
        self.starts.append(start)
        return dict(numberMatched=len(self.keys), features=[feature(k) for k in self.keys[start:start + pull_ryhti.PAGE]])


class Pull(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.d.name, 'r.csv')
        p = mock.patch.object(pull_ryhti, 'PAGE', 3)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.d.cleanup)

    def keys(self):
        with open(self.out, encoding='utf-8', newline='') as f:
            return [r['address_key'] for r in csv.DictReader(f)]

    def test_pages_to_the_end(self):
        svc = Service(['k%02d' % i for i in range(7)])
        self.assertEqual(pull_ryhti.pull(self.out, '1,2,3,4', get=svc), (7, 7, 7))
        self.assertEqual(svc.starts, [0, 3, 6])
        self.assertEqual(self.keys(), ['k%02d' % i for i in range(7)])
        self.assertFalse(os.path.exists(self.out + '.partial'))

    def test_resumes_at_the_last_whole_page(self):
        keys = ['k%02d' % i for i in range(8)]
        with self.assertRaises(KeyboardInterrupt):
            pull_ryhti.pull(self.out, '1,2,3,4', get=Service(keys, fail_at=2))
        with open(self.out + '.partial', 'a', encoding='utf-8') as f:
            f.write('half a row')        # a page cut short is dropped on resume
        svc = Service(keys)
        pull_ryhti.pull(self.out, '1,2,3,4', get=svc)
        self.assertEqual(svc.starts, [6])
        self.assertEqual(self.keys(), keys)

    def test_a_row_repeated_by_paging_is_kept_once(self):
        svc = Service(['a', 'b', 'c', 'c', 'd'])
        self.assertEqual(pull_ryhti.pull(self.out, '1,2,3,4', get=svc), (5, 4, 5))
        self.assertEqual(self.keys(), ['a', 'b', 'c', 'd'])


class Fetch(unittest.TestCase):
    def test_retries_a_network_error_and_a_5xx(self):
        answers = [urllib.error.URLError('reset'), urllib.error.HTTPError('u', 503, 'busy', {}, None),
                   io.BytesIO(json.dumps({'features': []}).encode())]

        def opener(url):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(pull_ryhti.fetch('u', pause=0, opener=opener), {'features': []})

    def test_gives_up_on_a_4xx_and_after_its_tries(self):
        def bad_request(url):
            raise urllib.error.HTTPError('u', 400, 'bad', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            pull_ryhti.fetch('u', pause=0, opener=bad_request)
        calls = []

        def down(url):
            calls.append(url)
            raise urllib.error.URLError('down')
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(urllib.error.URLError):
            pull_ryhti.fetch('u', tries=3, pause=0, opener=down)
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
