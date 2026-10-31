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
| Route guidance | works |
| Speed-limit signs | work |
| Address database and search | testing; builder not moved here yet |
| POI icons and search | work in the emulator; branch `pois` |
| House numbers along a street | testing; branch `house-ranges` |
| Signposts, names on unnamed ramps | untested; branches `signposts`, `ramp-names` |
| Lake, fjord and river names | not written |
| Showing the card's own map version | unsolved |

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

One TOML per region in `profiles/`. `profiles/iceland.toml` is the only one built so far.
`python3 tools/clarion_profile.py iceland` prints what the tools will read.

## Limits

- The firmware rejects positions outside its shipment area (longitude -20 to 68 on European
  units). The map draws but the car never appears. A NAND patch fixes it; the card can't.
- Only Iceland's four cells have been built. On a cell that already has data, the coarse
  levels are replaced, not merged.
- North of about 60° the 640-160 km views look stretched east-west, as on factory cards.

## Docs

| File | What |
|---|---|
| [FORMAT.md](docs/FORMAT.md) | card files, tile grid, tile header, index |
| [GEOMETRY.md](docs/GEOMETRY.md) | tile records, section by section |
| [BUILD-LOG.md](docs/BUILD-LOG.md) | what the unit accepted and refused |
| [ROAD-CLASSES.md](docs/ROAD-CLASSES.md) | road widths and the OSM mapping |
| [LEVEL5.md](docs/LEVEL5.md) | the coarse levels |
| [ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md) | plan for a generic address importer |

## Licence

Code: GPL-3.0-or-later ([LICENSE](LICENSE)). Maps built from OSM: ODbL 1.0. Factory card
data: commercial, not covered. Details in [LICENSES.md](LICENSES.md); tests and what not to
commit in [CONTRIBUTING.md](CONTRIBUTING.md).
