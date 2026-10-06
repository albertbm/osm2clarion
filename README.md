# osm2clarion

Build Clarion navigation tiles from OpenStreetMap, for Clarion QY8x head units.

The QY8x is a Windows CE navigation unit fitted to a number of cars through the 2010s. The map
format belongs to the unit, not to any car, so a card built here works in any of them. Two
database vintages were measured for this: a 2014 and a 2017 European card. The tools write for
both. The earlier QY7 is a different platform — SH-4, a monolithic map, no `MAPAL001` grid —
and its data is not this format.

No documentation of the format exists. Everything in [docs/](docs/) was read off real files and
checked by rebuilding them byte for byte.

## What it builds

The map surface. Sea, land and the coastline at every zoom from 640 km down to 50 m, and roads
with names at the levels that draw them. The output goes into the tiles of a European base
card, alongside the data already there, plus the two files that index them:

```
MAPAL001/B58R078R.DAT …   one tile per 8×8-mesh grid cell, levels 1 and 2
MAPAL001/LEV3456R.DAT     levels 3 to 6 for the whole database, edited in place
MMAIN001.DAT              the 4 KiB master file: database bounds and level counts
DRSTR001.DAT              the directory index, which the unit reads instead of scanning
```

It does not build the address database. There is no address search, no postcode lookup and no
house numbers for anything you add — `REFER001` is left exactly as the card shipped it. Routing
graphs are written at level 1 only, so the router will not route over new roads.

## What you need

- Python 3.11 or later, for `tomllib`.
- `shapely`. Everything that touches geometry needs it.
- `pyosmium`, but only for the two scripts that read a `.pbf`. Skip it and supply the
  coastline and road JSON some other way.
- An OSM extract of the region, as `.osm.pbf`. Geofabrik publishes them per country.
- **A base card.** The unit's own SD card, or an image of one, mounted read-only. The chain
  reads four things from it: `MAPAL001/LEV3456R.DAT`, any one tile as a header template,
  `MMAIN001.DAT`, and `DRSTR001.DAT` for the directory order. No map data from the card is
  redistributed, and none of it is in this repository.

```
pip install shapely pyosmium
```

## Running it

```
python3 build.py --profile iceland --base /path/to/card --out out --only extract
python3 build.py --profile iceland --base /path/to/card --out out
```

The first call reads the `.pbf` into a coastline and a road set; it is separate because it is
the one step that needs `pyosmium` and the one you repeat least. The second runs the rest. On a
laptop the whole thing takes a couple of minutes.

`out/` then holds the files to copy onto a card. Copy them, and nothing else — the index stores
every file's size, so regenerate `DRSTR001.DAT` whenever a file changes size.

Each step prints the command it runs, so any one of them can be repeated by hand:

```
python3 build.py --list                      the step names
python3 build.py … --from roads              resume part-way
python3 build.py … --only coarse             one step
```

The steps are `extract`, `coarse` (levels 3 to 6 of `LEV3456R.DAT`), `background` (one tile per
cell, sea and land), `roads` (levels 1 and 2 into those tiles), `master` and `index`.

## Profiles

Everything specific to a region lives in one TOML file under `profiles/`, read by
`tools/clarion_profile.py`. [profiles/iceland.toml](profiles/iceland.toml) is commented and is
the one profile that has been built. It holds:

| section | what |
|---|---|
| `[source]` | where the OSM extract and the derived coastline and road JSON live |
| `[grid]` | the grid cells the region occupies, and the database box in arc-minutes |
| `[shift]` | how far west to move the grid, for a region outside the shipped box |
| `[roads]` | simplify tolerance, which OSM classes to skip, which reach level 2, whether to write level-2 graphs |
| `[land]` | the smallest island kept and the simplify tolerance, per level |
| `[labels]` | the sea label, and text labels for the coarse levels |
| `[targets]` | which database vintages to build for |

Only three of those are really region-specific: the cells, the box and the shift. The
thresholds, the tolerances and the OSM-class mapping are general, and the defaults in
`clarion_profile.py` are what the measurements in `docs/` support.

Dump what the tools will see:

```
python3 tools/clarion_profile.py iceland
```

### The shift

A region west of 20° W or east of 68° E falls outside the box a European card ships with. The
obvious move is to widen the level-4 and level-5 directories, and the unit refuses it: it
assumes 44 level-4 columns and 11 level-5 columns and never reads the count. So `[shift]` moves
the whole grid west instead, by whole 8-mesh blocks, and every directory keeps the number of
columns it shipped with. For Iceland that is 8°, and the 8° dropped off the eastern end held no
level-4 records and eleven level-5 sea parcels over Kazakhstan. Anyone mapping the Azores or
Svalbard needs the same thing. A region already inside the box leaves the section out.

## The shipment bound

A unit only accepts positions inside the shipment area baked into its firmware. For a European
unit that area is longitude −20 to 68. Build a map outside it and the unit draws the map
correctly and then refuses every position, silently: the coastline is right, the roads are
right, the vehicle never appears, and nothing logs an error. Nothing on the card changes this —
the card's own declared bounds have no bearing on it, which is worth knowing before you spend a
day patching them. Recognise it for what it is and plan around it. Iceland sits west of −20, so
the four cells here are outside, and that is why the Iceland profile is useful to look at and
unsatisfying to drive.

## What is untested

**Only four cells have ever been built**, the four over Iceland, and they were the easy case:
their level-4 and level-5 parcels shipped empty or sea-only, so there was nothing to preserve.
`lev_coarse.py` replaces a coarse parcel outright, and nothing has checked what that does where
the shipped parcel already holds a populated European cell. Expect to lose whatever was there,
and possibly worse. The level-3 and tile levels are built fresh per cell and should be fine.

Other things stated as limits rather than bugs:

- Level-5 roads (the 64 km view) are added by `lev5_roads.py` after the fact, because the
  level-5 records this chain builds carry a background block and no road block.
- There are no coarse labels. Neither measured card carries any, and the country-badge table
  in the unit's firmware has no entry to borrow.
- The level-6 sheet is plain degrees and the unit draws it as such, so a northern region looks
  about twice too wide from 640 km to 160 km. Norway does the same. Nothing to fix in the data.
- `tools/make_drstr_card.py` needs the card mounted, because the index carries file sizes and
  takes its directory order from the card's own copy.

If you build a populated European cell and find what breaks, a pull request is welcome.

## Layout

```
build.py            run the whole chain for one profile
profiles/           one TOML per region
tools/              the chain, and the readers and encoders under it
docs/               the format, measured
```

| doc | what |
|---|---|
| [docs/FORMAT.md](docs/FORMAT.md) | the card: its files, the tile grid, the tile header, the directory index |
| [docs/GEOMETRY.md](docs/GEOMETRY.md) | the tile container and its records, section by section |
| [docs/BUILD-LOG.md](docs/BUILD-LOG.md) | what the unit accepted and what it refused, measured on hardware |
| [docs/ROAD-CLASSES.md](docs/ROAD-CLASSES.md) | how a road's width is chosen, and the OSM mapping |
| [docs/LEVEL5.md](docs/LEVEL5.md) | levels 4 and 5, the coarse views |

Several tools check themselves against real data. `clarion_tile.py` run on a tile rebuilds it
and compares; `clarion_bg.py` does the same per record; `tile_grid.py` checks the name-to-grid
formula across a directory of tiles; `make_drstr.py` reproduces a card's own index byte for
byte. Run them against a base card before trusting a change.

## Licence

Not chosen yet.
