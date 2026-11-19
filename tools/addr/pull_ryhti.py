#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pull the addresses in a box from Syke's Ryhti open_address service into a CSV.

usage: pull_ryhti.py OUT.csv LON_MIN LAT_MIN LON_MAX LAT_MAX
Data: Syke, Ryhti, CC BY 4.0. The whole country (19.0 59.7 31.6 70.1) is 3.9 million rows, about
780 pages one after another. Rows go to OUT.csv.partial a page at a time; a run that stops
resumes at the last whole page. A failed request is retried with a growing pause. At the end
rows are kept once per address_key (paging a live table can repeat a row) and the file is
renamed to OUT.csv.
"""
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

import certifi  # a python.org or uv interpreter has no system CA list

URL = ('https://paikkatiedot.ymparisto.fi/geoserver/ryhti_building/ogc/features/v1/'
       'collections/open_address/items')
COLUMNS = ('address_key building_key street street_swe number number2 letter letter2 '
           'postcode town municipality lat lon').split()
PAGE = 5000
TRIES = 8
CTX = ssl.create_default_context(cafile=certifi.where())


def row(f):
    p = f['properties']
    lon, lat = f['geometry']['coordinates'][:2]
    return [p['address_key'], p['building_key'], p['address_name_fin'], p['address_name_swe'],
            p['number_part_of_address_number'], p['number_part_of_address_number2'],
            p['subdivision_letter_of_address_number'], p['subdivision_letter_of_address_number2'],
            p['postal_code'], p['postal_office_fin'], p['municipality_number'], lat, lon]


def fetch(url, tries=TRIES, pause=5, opener=None):
    """The JSON at url; retried on a network error or a 5xx answer."""
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=180, context=CTX))
    for n in range(tries):
        try:
            return json.load(opener(url))
        except urllib.error.HTTPError as e:
            if e.code < 500 or n == tries - 1:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if n == tries - 1:
                raise
            err = e
        wait = pause * 2 ** n
        print('  retry %d after %s, in %d s' % (n + 1, err, wait), file=sys.stderr, flush=True)
        time.sleep(wait)


def page_url(bbox, start):
    return '%s?f=json&limit=%d&startIndex=%d&bbox=%s&sortBy=address_key' % (URL, PAGE, start, bbox)


def resume_point(part, mark):
    """(rows in whole pages, byte length) of an earlier run's file, from its mark file."""
    if not (os.path.exists(part) and os.path.exists(mark)):
        return 0, 0
    start, size = map(int, open(mark).read().split())
    return start, size


def pull(out, bbox, get=fetch):
    part, mark = out + '.partial', out + '.partial.mark'
    start, size = resume_point(part, mark)
    if start:
        print('resuming at row %d' % start, flush=True)
        with open(part, 'r+b') as f:
            f.truncate(size)        # drop a page cut short
    else:
        with open(part, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(COLUMNS)
    matched = None
    while True:
        d = get(page_url(bbox, start))
        matched = d.get('numberMatched', matched)
        feats = d['features']
        with open(part, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerows(row(x) for x in feats)
        start += len(feats)
        with open(mark, 'w') as f:
            f.write('%d %d' % (start, os.path.getsize(part)))
        if start // PAGE % 20 == 0 or len(feats) < PAGE:
            print('%d of %s rows' % (start, matched), flush=True)
        if len(feats) < PAGE:
            break
    seen, kept = set(), 0
    with open(part, encoding='utf-8', newline='') as f, open(out + '.tmp', 'w', newline='', encoding='utf-8') as g:
        r, w = csv.reader(f), csv.writer(g)
        w.writerow(next(r))
        for x in r:
            if x[0] not in seen:
                seen.add(x[0])
                w.writerow(x)
                kept += 1
    os.replace(out + '.tmp', out)
    os.remove(part)
    os.remove(mark)
    print('%d rows pulled, %d once per address_key, the service counted %s' % (start, kept, matched))
    return start, kept, matched


if __name__ == '__main__':
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    pull(sys.argv[1], ','.join(sys.argv[2:6]))
