<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Build steps

What `build.py` runs, step by step, and the options of the tools behind each step. Every
step prints its command, so any of them can be run again by hand.

## Generate mode (Iceland)

| step | what it does |
|---|---|
| `prep` | writes the JSON config the tools read (`--work`/config/NAME.json) from the profile and the local file |
| `cards` | `build_gen_lev.py`: every map file, then a copy of the base card with them written on |
| `poi` | `poi_apply.py`: the speed cameras the `cards` step extracted, the dealer icon, the building-scale icon and the dealer's POINT record |
| `icons` | `build_poicap.py`: POI icons and POINT search records from the OSM extract |
| `addr` | `build_poicap.py --house-ranges ALL --all-streets`: the address database with house-number ranges |
| `consent` | the unit's `consent_cmd` from the local file, if it has one; a unit with `consent = "required"` in the profile fails without one |
| `check` | needs every step of the chain in the manifest and the card to be the image the last one wrote, then runs `card_check.py` (road graph and coarse file) and `card_verify.py` (DATA VERSION, index, CMP.LST, MMAIN, towns, inventory); renames the card on a pass and prints the cameras, dealer and consent the manifest records |

With `--map-from` or `--restamp-from` the chain is `prep`, `map` or `restamp`, `consent`,
`check`.

Known limit: the check proves a card's structure, not where each file came from. A well-formed
tile or ADDRE of another build of the same unit, written after the `cards` step, would pass;
today only the emptied stage dirs keep one out.

## Area mode (Finland)

| step | what it does |
|---|---|
| `factory` | copies the base card's map files into `--work`, clearing those of an earlier base |
| `osm` | turn restrictions and the area's roads from the OSM extract |
| `strip` | `road_strip.py`: removes the roads from every tile and from the coarse file |
| `roads` | `rd_build.py`: the area's roads into its one cell |
| `coarse` | `lev_add_roads.py`: coarse roads, and the level-2 graph when `coarse_graph` is set |
| `sec4` | `sec4_write.py`: section-4 country records on the rebuilt tile |
| `master` | `make_mmain.py`: MMAIN001.DAT from the tile headers |
| `card` | a copy of the base card with the tiles, the coarse file and MMAIN written on |
| `check` | reads every written file back off the image, runs `card_check.py` on the rebuilt cell and the coarse file (never on a factory tile), then `card_verify.py`'s version, index and MMAIN checks; renames the card on a pass |

## build_gen_lev.py

```
build_gen_lev.py --config PATH --unit NAME --slug NAME BUILD_DIR [--base IMG] [--what TEXT]
                 [--coast PATH] [--reuse] [--number N] [--partial]
                 [--map-from CARD | --restamp-from CARD]
                 [--roads [--also NAME,NAME] [--to NAME,NAME [--by time]] | --area NAME[,NAME...] [--coarse-graph]]
                 [--write lev|tiles,addr] [--addr-only NAME,NAME]
                 [--landuse lon0,lat0,lon1,lat1 [--landuse-kinds forest,water,...] [--buildings LAYER:CLASS]
                  [--house]]
```

It builds the generated map files into BUILD_DIR, puts them on a fresh copy of the unit's base
card and writes the card's manifest. The card then gets the generated LEV3456R.DAT (old
clusters zeroed), its DRSTR size, MMAIN's level counts summed from every MAPAL001 header, and a
CMP.LST made from the card's own .cmp files.

The manifest covers every file in MAPAL001, REFER001, REFER002, HOUSE001 and the root index
files. Each is one of:

| status | meaning |
|---|---|
| generated | byte-identical to a step's output; the step's command and inputs are listed |
| edited | the base card's file with named fields rewritten by this build |
| leftover | no step produced it |

`--config` names the build's config file (unit table, areas, cells, data paths, card naming),
which build.py writes from the profile. `--unit` picks the DATA VERSION every generated file
carries (config `units.*.version`; the firmware checks it against the one in its own memory)
and the card `build_icode_clean.py` reads its two ICODE001.DAT files from (config
`units.*.icode`). The card is written under CARDS/builds following
`card_naming.build_pattern`, with its manifest beside it, under the number `--number` gives.
A row goes to the top of the cards log (config `cards_log`), status untested; `--what` is its
"what changed" text (default: the slug). `--reuse` skips steps whose inputs, tools and
library versions are unchanged. `--partial` writes the card and its manifest with a .partial
suffix and leaves out the log row; build.py renames them once its check step passes.

`--map-from CARD` writes the map, address and POI files of a finished card onto this unit's
base as they are: nothing is generated, restamped or recompressed, so the source must carry
this unit's DATA VERSION. The ICODE files that number its towns go with them. The target keeps
its own software and settings and gets a refreshed index.

`--restamp-from CARD` does the same with this unit's DATA VERSION: every stamp in those files is
rewritten (packed files are unpacked, stamped and repacked) and the icode step runs on this
unit's ICODE input. That is all that differs between the units' full builds, so one full build
gives the other units' cards.

`--area` builds named areas: every drivable public road in their relations (config `areas`)
into the areas' road cells, all given section 4; coarse roads at levels 3 to 5 in
LEV3456R.DAT; and every address of those municipalities out of the address CSV (config
`data.addresses` and `data.postcodes`, `addr_csv.py`). `--addr-only NAME,NAME` keeps only those
streets in ADDRE; towns and postcodes stay whole.

Before the map steps, the turn restrictions, the coastline and the speed cameras are read off
the OSM extract into BUILD_DIR/osm, each a stamped step written under a temp name and then
moved, so a newer extract reruns them and the same one reuses them. The cameras step runs
`extract_cameras.py` with the boxes of the areas `--area` names (all areas without it):
`highway=speed_camera` nodes and `enforcement` relations. Each device node is a camera, and a
device on an average-speed section gets one camera per direction of travel. No map
step reads the cameras, so a rerun of that step reruns nothing else. `poi_apply.py` writes
them onto the road tiles; a camera with no `maxspeed` takes the speed of the built road
nearest it, its `maxspeed` or else the class default the road links carry, and the step's
output counts the cameras that got a class default. `[build] cameras = false` in the profile
or the local file leaves the step out and the card without cameras.

`--roads` is the older path mode: `gen_roads_path.py` picks the ways between the profile's
`[roads_poc]` streets (`--also` adds whole streets, `--to` a route to each named street, `--by`
how that route is weighed). build.py does not use it.

`--write` names what the assemble step puts on the card (default `lev`); the rest is only
checked against the card.

`--landuse` adds a step that cuts OSM land use (forest, park, lakes; `extract_landuse.py`) to
the box, and the tiles step draws it under the roads. `--landuse capital` names that area's box;
`--landuse area` uses the union of the areas `--area` selects. `--landuse-kinds` narrows the
kinds. `--buildings LAYER:CLASS` adds OSM building footprints in that section-1 layer and class;
it is a trial, since no class for buildings is known on European cards.

`--house` writes the landuse box as HOUSE001 files, the map the unit draws at its building-scale
zoom: merged building blocks, green areas, water and the sea, and buffered road polygons
(`house_encode.py`). Per-file counts land in house/stats.json. Only cells inside a populated
area are written: a place node buffered `--house-city-buffer` metres (default 400), or a
residential, commercial, industrial or retail landuse polygon buffered `--house-landuse-buffer`
metres (default 100).
