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

Only Iceland has been built: a country added to a card that lacked it. Updating a country
the card already has is untested.

## Needs

- Python 3 with `shapely`, and `osmium` for reading `.pbf`.
- An OSM extract (`.osm.pbf`), e.g. from Geofabrik.
- A base card or a copy of one. Read only: `MAPAL001/LEV3456R.DAT`, any one
  `MAPAL001/*.DAT` as a header template, `MMAIN001.DAT`, and `DRSTR001.DAT`.

## Run

```
python3 build.py --profile iceland --base CARD --out DIR
python3 build.py --list            # steps: extract coarse background roads master index
python3 build.py ... --only STEP   # one step
python3 build.py ... --from STEP   # resume
python3 build.py ... --target v2014
```

Each step prints the command it runs. Copy the files in `DIR` onto the card. Rebuild
`DRSTR001.DAT` whenever a file changes size; it stores sizes.

## Profiles

One TOML per region in `profiles/`. Iceland builds a card from scratch; Finland rebuilds one area on a stock card (below).
`python3 tools/clarion_profile.py iceland` prints what the tools will read.

## Finland on a QY7 card

For a stock European Clarion QY7221NL card (2014 database) and a Geofabrik
`finland-latest.osm.pbf`. The card keeps its own map. The roads are stripped from every
tile, and the roads of one town are rebuilt from OSM. `profiles/finland.toml` names the
town (`jyvaskyla`) and everything else the build needs.

```
uv run --with osmium --with shapely python build.py --profile finland \
    --base STOCK-CARD.img --osm finland-latest.osm.pbf --out WORKDIR --card NEW-CARD.img
```

`--base` is the card image, only read. `--card` is a copy of it with the new files written
in and `DRSTR001.DAT` refreshed; leave it out to get just the files in `WORKDIR`
(`MAPAL001/*.DAT`, `MMAIN001.DAT`). `--area NAME` picks the town if the profile lists more
than one. Steps: `--list`, `--only STEP`, `--from STEP`. `WORKDIR` takes about 4.6 GiB and
the card image is 7.4 GiB. A second area is one more `[areas.NAME]` table: an OSM relation and
the one tile cell it sits in.

Limits:

- Roads exist only inside the built area. Elsewhere in Europe the card draws the map but no
  roads, and cannot route.
- Address search still reads the factory data, which points at roads that are now gone. It is
  likely to crash.
- Guidance has not been tried on a unit.

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
