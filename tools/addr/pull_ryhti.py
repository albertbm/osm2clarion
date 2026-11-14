#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pull the addresses in a box from Syke's Ryhti open_address service into a CSV.

usage: pull_ryhti.py OUT.csv LON_MIN LAT_MIN LON_MAX LAT_MAX
Data: Syke, Ryhti, CC BY 4.0. Pull a box; the whole country is 3.9 million rows.
"""
import csv
import json
import ssl
import sys
import urllib.request

import certifi  # a python.org or uv interpreter has no system CA list

URL = ('https://paikkatiedot.ymparisto.fi/geoserver/ryhti_building/ogc/features/v1/'
       'collections/open_address/items')
COLUMNS = ('address_key building_key street street_swe number number2 letter letter2 '
           'postcode town municipality lat lon').split()
PAGE = 5000
CTX = ssl.create_default_context(cafile=certifi.where())


def rows(bbox):
    start = 0
    while True:
        u = '%s?f=json&limit=%d&startIndex=%d&bbox=%s&sortBy=address_key' % (URL, PAGE, start, bbox)
        feats = json.load(urllib.request.urlopen(u, timeout=120, context=CTX))['features']
        for f in feats:
            p = f['properties']
            lon, lat = f['geometry']['coordinates'][:2]
            yield [p['address_key'], p['building_key'], p['address_name_fin'], p['address_name_swe'],
                   p['number_part_of_address_number'], p['number_part_of_address_number2'],
                   p['subdivision_letter_of_address_number'], p['subdivision_letter_of_address_number2'],
                   p['postal_code'], p['postal_office_fin'], p['municipality_number'], lat, lon]
        if len(feats) < PAGE:
            return
        start += PAGE


if __name__ == '__main__':
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    with open(sys.argv[1], 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows(','.join(sys.argv[2:6])))
