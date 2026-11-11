# osm2clarion

Builds map cards for Clarion QY8x navigation units from OpenStreetMap, from 640 km down to
building scale (25 m). Nissan no longer ships map updates for these units.

Tested only on QY8x units in the Nissan Leaf 2016-2019 (QY8202NA, QY8602NB, QY8600NE) and
the Nissan e-NV200 (QY8202PB), European and US market. The format is undocumented;
[docs/](docs/) was measured off 2014 and 2017 European cards. No card data ships here.

## Status

| Feature | State |
|---|---|
| Coast, land, roads, road names | works |
| Route guidance, including long routes on the coarse graph | works |
| Roundabout ring markers | works |
| Speed-limit signs | works |
| Speed cameras from OSM | extractor and writer exist, not wired into the Iceland build |
| Address database and search | works: street search, City-first, house numbers, tested on the bench and in a car |
| POI icons and search | works |
| House numbers along a street | works |
| Signposts, names on unnamed ramps | built and unit-tested, not wired into the Iceland build |
| Lake, fjord and river names | not written |
| Showing the card's own map version | unsolved |

Addresses come from a CSV import (Iceland's own national registry, or any source in the
same shape) or from OSM; see [ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md).

Iceland is a country added to a card that lacked it. Finland replaces the roads of one town on
a card that already has the country. Updating a whole country is untested.

## Build a card

One command builds every card. The profile in `profiles/NAME.toml` holds everything that
differs between regions: the areas, the cells, the units and their DATA VERSION, what gets
written, and where the data files are.

```
uv run --with osmium --with shapely python build.py --profile NAME \
    --base BASE-CARD.img --out OUT.img
```

`--base` is read, never written. `--work DIR` holds what is generated (default `build/` followed by
the name of `--out`). `--list` names the steps, `--only STEP` runs one and `--from STEP`
resumes at one. Each step prints the command it runs. Both builds need `shapely` and `osmium`
for Python; the `uv run` above fetches them.

Paths and values that belong to one machine go in a local file, merged over the profile table
by table. The profile's `[build] local` names it (`local/NAME.toml`, ignored by git), and
`--local FILE` overrides that. Relative data paths in the profile are read from the repository
root.

### Iceland, QY8

`[build] mode = "generate"`. The card is built from scratch on the unit's base card:
background, land use, roads with turn restrictions, the coarse file with its graph, the
building-scale map, addresses and POI icons, for every `[areas.NAME]` table (`--area a,b`
picks some). `--out` is `CARDS/builds/IS-NNN-UNIT-SLUG.img`: the unit and the slug come from
that name, `CARDS` is two directories up, and the card list there (`cards_log`) gets a row.
`--what TEXT` is that row's description, `--prev IMG` a card to compare against at the end.

The profile needs, per unit, a `[units.UNIT]` table with the `version` the unit checks. The
local file adds `icode` (the card the ICODE files are read from; the base card when left out),
an optional `consent_cmd` run on the finished image (`{img}` stands for it), the `[data]`
paths, a `[dealer]` address and `poi_python`, the interpreter for the POI steps.

`[data]` holds `pbf`, `turns`, `coast`, `addresses`, `postcodes` and `cameras`. The turn
restriction and coastline files are made from the extract when they are missing. The address
and postcode files are Iceland's national registry in CSV; see [ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md).

### Finland, QY7

`[build] mode = "area"`. For a stock European Clarion QY7221NL card (2014 database) and a
Geofabrik `finland-latest.osm.pbf`. The card keeps its own map. The roads are stripped from
every tile, and the roads of one town are rebuilt from OSM and written into a copy of the
image. `profiles/finland.toml` names the town (`jyvaskyla`).

```
uv run --with osmium --with shapely python build.py --profile finland \
    --base STOCK-CARD.img --osm finland-latest.osm.pbf --out NEW-CARD.img
```

`--area NAME` picks the town if the profile lists more than one. `--work` takes about 4.6 GiB
and the card image is 7.4 GiB. A second area is one more `[areas.NAME]` table: an OSM
relation, the one tile cell it sits in, and a bbox. The `[grid]` cells are left out of the
profile: build.py takes every tile on the base card that the `[grid]` boxes touch.

Limits of the Finland card:

- Roads exist only inside the built area. Elsewhere in Europe the card draws the map but no
  roads, and cannot route.
- Address search still reads the factory data, which points at roads that are now gone. It is
  likely to crash.
- Guidance has not been tried on a unit.

## Profiles

One TOML per region in `profiles/`. `python3 tools/clarion_profile.py iceland` prints what
the tools will read.

## Limits

- The firmware rejects positions outside its shipment area (longitude -20 to 68 on European
  units). The map draws but the car never appears. A NAND patch fixes it; the card can't.
- Stand-alone Iceland cards are built from scratch, own tiles and own coarse file. Adding
  a country to a card that already carries others is untested; the coarse file is written
  fresh, not merged into what is there.
- Building outlines (25 m scale) are only drawn in populated areas - a place node or
  residential/commercial/industrial/retail landuse within a couple hundred metres. A house
  out in the open country gets no shape even where OSM has its footprint.
- North of about 60° the 640-160 km views look stretched east-west, as on factory cards.
- In drive simulation, the 100 m and 200 m views stop drawing the route a few kilometres
  from where it was calculated, at the edge of a 5x5 block of level-1 parcels around that
  point; the unit recalculates once the car gets there. The 500 m and wider views show the
  whole route. It showed up once builds covered more than one town and doesn't affect real
  driving. Setting the level-1 link-direction flag and the two cost-word bits that factory
  tiles carry on links an upper level covers did not change it.

## Docs

| File | What |
|---|---|
| [FORMAT.md](docs/FORMAT.md) | card files, tile grid, tile header, index |
| [GEOMETRY.md](docs/GEOMETRY.md) | tile records, section by section |
| [BUILD-LOG.md](docs/BUILD-LOG.md) | what the unit accepted and refused |
| [ROAD-CLASSES.md](docs/ROAD-CLASSES.md) | road widths and the OSM mapping |
| [LEVEL5.md](docs/LEVEL5.md) | the coarse levels |
| [ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md) | addresses: bring your own source (CSV or OSM) |

## Licence

Code: GPL-3.0-or-later ([LICENSE](LICENSE)). Maps built from OSM: ODbL 1.0. Factory card
data: commercial, not covered. Details in [LICENSES.md](LICENSES.md); tests and what not to
commit in [CONTRIBUTING.md](CONTRIBUTING.md).
