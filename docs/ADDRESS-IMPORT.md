<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Addresses: bring your own source

`addre_io`, `zcode_io` and `icode_io` write an address database onto a card.
`tools/addr/addr_csv.py` reads the rows. `[addr] source` in the profile picks where they come
from: `"register"`, a national address register's CSV at `[data] addresses` in the layout
`[addr] register` names, or `"osm"`, the `addr:*` tags of the OSM extract.

| source | register | reads | from |
|---|---|---|---|
| `register` | `stadfangaskra` | Iceland's address register | `[data] addresses`, `[data] postcodes` |
| `register` | `ryhti` | a CSV pulled from Finland's Ryhti open_address service | `[data] addresses` |
| `osm` | | `addr:*` tags on nodes, closed ways and multipolygon relations | `[data] pbf` |

```toml
[addr]
source = "register"
register = "ryhti"

[data]
addresses = "build/ryhti-finland.csv"
```

Iceland and Finland both read their registers. The older single key, `format = "osm"` or
`format = "<register>"`, still reads; a table may not set both. Another country needs one
more reader that gives the same rows. The flat shape below describes what such a reader has
to deliver.

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

`source = "osm"` reads `addr:street` and `addr:housenumber` (and `addr:postcode`,
`addr:city` where present) from the pbf, inside `[addr] box`. A house number such as
`12a` or `12 A` splits into number 12 and letter A; a range keeps its first number. The
municipality is not looked up. `towns()` takes the town written most often beside each
postcode.

The Overpass queries below give the same rows for a place you have no pbf for.

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

## Source 3: Ryhti (Finland)

`register = "ryhti"` reads a CSV of Finland's building addresses. The data is from Syke's
Ryhti service (`open_address` at
`paikkatiedot.ymparisto.fi/geoserver/ryhti_building/ogc/features/v1/`), licensed CC BY 4.0;
credit "Lähde: Syke Ryhti" (see LICENSES.md). The columns are `street`, `street_swe` (the
Swedish name where the municipality has one), `number`, `number2` (end of a range), `letter`,
`letter2`, `postcode`, `town`, `municipality` (three digits, becomes `svf`), `lat` and `lon`,
with `address_key` and `building_key` kept for reference. The service returns WGS84 and no
reprojection is needed. Rows with no street or number are skipped.

`tools/addr/pull_ryhti.py OUT.csv LON_MIN LAT_MIN LON_MAX LAT_MAX` pulls a box. The whole
country (`19.0 59.7 31.6 70.1`) is 3.9 million rows, about 780 requests made one after
another; a stopped pull starts again at its last whole page, and a failed request is retried.

## A factory card with one rebuilt area (QY7)

An area-mode card keeps the factory area tree, so there is no ICODE, ZCODE or POINT to
write. `tools/addr/build_addr_country.py` rewrites one file, the country's `ADDRE`, from the
addresses inside the area's relations:

- with `[areas.NAME] municipality`, the ladder is the row it names one level above the
  deepest tier of the card's own `ICODE001.DAT`, plus the settlement under it that the
  address's town names. An address whose town is no settlement row goes to the settlement
  named like the municipality;
- without one, the whole country. Each address goes to the kunta its municipality number
  names (the OSM boundary with that `ref`, on its Finnish or Swedish name; a kunta merged
  away since the card was made is picked by the nearest factory record), then to the
  settlement where the factory file has the street: the only one, or the one of the nearest
  record of that name. A street the factory never had takes the settlement of the nearest
  factory record in the kunta, which gives the factory's answer for 92.7% of a held-out
  sample. `tools/addr/settle.py` has the rules;
- a Ryhti row with a Swedish name as well is found under both names, as the factory file
  lists MANNERHEIMINTIE and MANNERHEIMVÄGEN. The factory labels such a road
  `MANNERHEIMINTIE:MANNERHEIMVÄGEN`; ours carry OSM's `name` alone, so the name not on the
  label is left out and listed in `addr_other_language.json`;
- a street gets one row per road link and side with the house numbers along it, as
  `--house-ranges ALL` writes for Iceland, or a plain row when it has no numbers;
- a street with no road of its name in the rebuilt tile is left out (`addr_skipped.json`);
- the file is the card's own layout. A QY7 card's is described at the top of `addre_io.py`.

With a municipality, street search for the rest of that country then finds nothing, where
the factory file pointed at roads the build removed.
