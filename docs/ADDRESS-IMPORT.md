<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Addresses: bring your own source

`addre_io`, `zcode_io` and `icode_io` write an address database onto a card. The one
reader today is `tools/addr/addr_csv.py`, for Iceland's address register. Another
country needs a reader that gives the same rows: a column mapping in `addr_csv.py` for
a register like Iceland's, or a new reader. Nothing reads the flat shape below or OSM
addresses yet; they describe what such a reader has to deliver.

## The shape

One row per address, country code, up to four administrative levels (outermost
first, blank where the country has fewer), street, house number, suffix,
postcode, latitude and longitude:

```
country_iso,level1,level2,level3,level4,street,number,suffix,postcode,lat,lon
352,Höfuðborgarsvæðið,Reykjavík,,,Kirkjustræti,14,,101,64.14667,-21.94025
```

Levels run outermost first and may be blank. The ladder's depth is whatever the
data has, not a fixed set of names: Andorra is 3 deep, the UK 4, Italy 5, and
`df[0x49]` has to match. Calling the columns `level1..n` rather than `region`,
`province` and `city` keeps the shape out of the business of deciding what a
country's divisions are called.

Coordinates are per address and required. A source with none needs a join
against road geometry first - the caller's problem, not this repo's.

The tier labels themselves can't come from the rows. The ISO entry carries
seven slots of text - `COUNTRY / PARROQUIA / SETTLEMENT` for Andorra,
`COUNTRY / REGION / PROVINCE / COMMUNIONE / BUILT-UP AREA` for Italy - and the
unit shows them as given, so they're a small config beside the address rows,
not something derived.

## Source 1: a CSV

Iceland's own source is Staðfangaskrá, the national address registry (CSV,
plus a `;`-delimited `postnumeraskra` file mapping postcodes to towns). The
columns that matter: `HEITI_NF` (street, nominative case), `HUSNR` (house
number), `BOKST` (letter), `POSTNR` (postcode), `SVFNR` (municipality code),
`N_HNIT_WGS84` / `E_HNIT_WGS84` (lat/lon). `tools/addr/addr_csv.py` reads it, and
`tools/addr/build_addr_area0.py` turns the rows into `ADDRE`, `ZCODE` and `POINT`
records through `addre_io.py`.

HMS (Húsnæðis- og mannvirkjastofnun) publishes `Stadfangaskra.csv` as open data on
fasteignaskra.is. The postcode list comes from Pósturinn (postur.is); save it as
`;`-separated CSV, postcode in the first column and town in the second.

A different country's registry will have different column names but the same
shape: street, number, postcode, town or municipality, coordinates. Map those
columns to the row above and the writers don't care where the data came from.

## Source 2: OSM

No tool reads OSM addresses yet. This is how a reader would get them.

Addresses tagged `addr:street` + `addr:housenumber` on nodes, ways or
relations give the same rows without a national source. An Overpass query for
one area:

```
[out:json];
area["name"="Reykjavík"]->.a;
(
  node["addr:street"]["addr:housenumber"](area.a);
  way["addr:street"]["addr:housenumber"](area.a);
  relation["addr:street"]["addr:housenumber"](area.a);
);
out center tags;
```

or by bounding box instead of a named area:

```
[out:json];
(
  node["addr:street"]["addr:housenumber"](64.08,-22.05,64.20,-21.65);
  way["addr:street"]["addr:housenumber"](64.08,-22.05,64.20,-21.65);
  relation["addr:street"]["addr:housenumber"](64.08,-22.05,64.20,-21.65);
);
out center tags;
```

`out center tags` gives a way or relation's centre point along with its tags,
which is enough to fill the row above (`addr:street`, `addr:housenumber`,
`addr:postcode`, `addr:city`, and the centre for lat/lon). The same tags can be
read offline from a local `.osm.pbf` with `osmium`, for an area too large for
one Overpass call.
