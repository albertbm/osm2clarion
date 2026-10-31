# osm2clarion

Build Clarion navigation tiles from OpenStreetMap, for the Clarion QY8x head units fitted to
a number of cars through the 2010s. The format belongs to the unit, not to any car, so a card
built here works in any of them. It is undocumented: [docs/](docs/) was measured off a 2014
and a 2017 European base card, and the tools write for both. No card data ships here.

## What it builds

Sea, land and the coastline from 640 km down to 50 m, plus roads with names at the levels
that draw them, into a European base card's own tiles alongside the data already there.

```
MAPAL001/B58R078R.DAT ...   one tile per 8x8-mesh grid cell, levels 1 and 2
MAPAL001/LEV3456R.DAT       levels 3 to 6 for the whole database, edited in place
MMAIN001.DAT                the 4 KiB master file: database bounds and level counts
DRSTR001.DAT                the directory index the unit reads instead of scanning
```

## What you need

- Python 3.11 or later, for `tomllib`. Not 3.14 yet: `osmium` has no wheel for it.
- `shapely`, for everything that touches geometry.
- `osmium`, only for the two scripts that read a `.pbf`. Supply their coastline and road
  JSON another way and you can skip it.
- An OSM extract as `.osm.pbf`. Geofabrik publishes them per country.
- A base card, mounted read-only: the unit's own SD card, or an image. Four files are read
  off it, none written back: `MAPAL001/LEV3456R.DAT`, any one tile as a header template,
  `MMAIN001.DAT`, and `DRSTR001.DAT` for the directory order.

## Running it

```
pip install shapely osmium
python3 build.py --profile iceland --base /path/to/card --out out --only extract
python3 build.py --profile iceland --base /path/to/card --out out

--list           the step names      extract      the .pbf into coastline and roads
--from roads     resume part-way     coarse       levels 3 to 6 of LEV3456R.DAT
--only coarse    one step            background   one tile per cell, sea and land
--target v2014   the 2014 database   roads        levels 1 and 2 into those tiles
                                     master       MMAIN001.DAT
                                     index        DRSTR001.DAT
```

`extract` is split off as the one step needing `osmium`. The rest takes a couple of minutes
on a laptop and leaves `out/` to copy onto a card. Copy those files and nothing else, and
regenerate `DRSTR001.DAT` whenever one of them changes size, because the index stores sizes.
Each step prints the command it runs, so it can be repeated by hand.

## Profiles

One TOML per region under `profiles/`, read by `tools/clarion_profile.py`.
[profiles/iceland.toml](profiles/iceland.toml) is commented, and is the one that has been
built. `python3 tools/clarion_profile.py iceland` dumps what the tools will see.

| section | what |
|---|---|
| `[source]` | the OSM extract, and the coastline and road JSON from it |
| `[grid]` | the cells the region occupies, and the database box in arc-minutes |
| `[shift]` | how far west to move the grid, outside the shipped box |
| `[roads]` | simplify tolerance, classes to skip, classes at level 2, level-2 graphs |
| `[land]` | smallest island kept, and simplify tolerance, per level |
| `[labels]` | the sea label, and text labels for the coarse levels |
| `[targets]` | which database versions to build for |

Only the cells, the box and the shift are region-specific. The rest are general, with
defaults the measurements in `docs/` support. A region west of 20° W or east of 68° E falls
outside a European card's box, and `[shift]` moves the grid west to reach it, for reasons
[docs/GEOMETRY.md](docs/GEOMETRY.md) gives.

## Limits

- Japanese-market cards are a different format (`DBDIR/`, a paged database keyed on JIS
  meshes). This fork reads them, and nothing writes one yet: [docs/JP-DBDIR.md](docs/JP-DBDIR.md).
- No address database. `REFER001` ships through untouched: no address search, no postcode
  lookup, no house numbers.
- Routing graphs at level 1 only. The router will not route over new roads.
- No coarse labels. Neither measured card carries any.
- Level-5 roads, the 64 km view, are a second pass by `lev5_roads.py`.
- The level-6 sheet is plain degrees, so a northern region looks about twice too wide from
  640 km to 160 km. Norway does the same.
- `tools/make_drstr_card.py` needs the card mounted.
- Untested past the four cells over Iceland, whose coarse parcels shipped empty or sea-only.
  `lev_coarse.py` replaces a coarse parcel outright, so expect a populated European cell to
  lose what was there. [docs/BUILD-LOG.md](docs/BUILD-LOG.md) has the detail.
- A unit refuses every position outside the shipment area in its firmware, longitude -20 to
  68 on a European one. The map draws; the vehicle never appears and nothing logs an error.
  No card edit changes it. Iceland sits west of -20, which is why its profile is useful to
  read and unsatisfying to drive.

If you build a populated European cell and find what breaks, a pull request is welcome.

## Layout

```
build.py     run the whole chain for one profile
profiles/    one TOML per region
tools/       the chain, and the readers and encoders under it
docs/        the format, measured
```

| doc | what |
|---|---|
| [FORMAT.md](docs/FORMAT.md) | the card: its files, the tile grid, the tile header, the index |
| [GEOMETRY.md](docs/GEOMETRY.md) | the tile container and its records, section by section |
| [BUILD-LOG.md](docs/BUILD-LOG.md) | what the unit accepted and refused, measured on hardware |
| [ROAD-CLASSES.md](docs/ROAD-CLASSES.md) | how a road's width is chosen, and the OSM mapping |
| [LEVEL5.md](docs/LEVEL5.md) | levels 4 and 5, the coarse views |

Several tools check themselves against real data; the table in
[GEOMETRY.md](docs/GEOMETRY.md) says which. Run them against a base card first.

## Licence

The tools here are GPL-3.0-or-later; the text is in [LICENSE](LICENSE). A map built from
OpenStreetMap is ODbL 1.0 instead, and the shipped navigation database on a factory card is
licensed commercial data that nothing here covers. [LICENSES.md](LICENSES.md) sets out all
three, and [CONTRIBUTING.md](CONTRIBUTING.md) covers the tests and what must not be committed.
