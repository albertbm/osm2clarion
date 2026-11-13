# osm2clarion

Builds map cards for Clarion QY7 and QY8 navigation units from OpenStreetMap, from 640 km
down to building scale (25 m). Nissan no longer ships map updates for these units.

Tested on QY8 units in the Nissan Leaf 2016-2019 (QY8202NA, QY8602NB, QY8600NE) and the
Nissan e-NV200 (QY8202PB), European and US market. QY7 cards (QY7221NL) have only run in an
emulator so far. The format is undocumented; [docs/](docs/) was measured off 2014 and 2017
European cards. No card data ships here.

Japanese units are not supported, QY7 or QY8. Their cards hold the map as one large file
instead of tiles.

## Status

| Feature | State |
|---|---|
| Coast, land, roads, road names | Works |
| Route guidance | Works; long routes are tested in the emulator, not yet in a car |
| Roundabouts | Works |
| Speed-limit signs | Works |
| Speed cameras | Works |
| Address search and house numbers | Works |
| House numbers drawn along streets | Works |
| POI icons and search | Works with bugs: in a municipality with no town of its own, a POI can show a nearby town |
| Names on unnamed ramps | Works |
| Signposts | Not built |
| Lake, fjord and river names | Not built |
| The card's own map version on screen | Not working |

Addresses come from a CSV import. The only reader today is for Iceland's address register;
another country needs its columns mapped in `tools/addr/addr_csv.py`. See
[ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md).

Iceland is a country added to a card that lacked it. Finland replaces the roads of one town on
a card that already has the country. Updating a whole country is untested.

## Install

- [uv](https://docs.astral.sh/uv/). `uv run build.py` makes a `.venv` with Python 3.12 and
  the shapely and osmium versions in `uv.lock`, and every step runs under it. Another shapely
  can move a point and change the card's bytes, so use the locked versions.
- macOS or Linux. On APFS (macOS), btrfs or XFS a card copy shares blocks with its base and
  costs almost nothing; elsewhere each copy is a full 16 GB.
- Disk: an Iceland build needs about 1 GB in `--work` plus the card (16 GB), and up to three
  card copies at once where copies cannot share blocks. Finland needs 4.6 GiB in `--work`
  and 7.4 GiB for the card.

```
git clone https://github.com/albertbm/osm2clarion
cd osm2clarion
uv run python -m unittest discover -s tests
```

## The base card

Every build starts from a full image of a card that runs in your unit. The build reads it and
never writes it. The image holds the unit's software and the factory map database, which is
licensed data: keep it to yourself.

To make one, take the SD card out of the unit, put it in a reader and copy the whole device,
not a partition. Find the device with `diskutil list` (macOS) or `lsblk` (Linux), then:

```
sudo dd if=/dev/rdisk4 of=IS-000-ZE1-base.img bs=4m      # macOS, rdiskN for disk4
sudo dd if=/dev/sdb of=IS-000-ZE1-base.img bs=4M         # Linux
```

Keep the image read-only (`chmod a-w`) and keep the card itself as it was.

The unit checks every map file's DATA VERSION against the one stored in its own memory, and
refuses a card that does not match. A build stamps the version that the profile's
`[units.UNIT]` table gives, so the base card must come from a unit of that type with that
version. The Iceland profile knows `14/11/17/01` (ZE0, 2014 database) and `17/09/01/01`
(ZE1 and ME0, 2017 database). To see what a base carries:

```
uv run tools/card_verify.py --card IS-000-ZE1-base.img --version 17/09/01/01
```

Only the `version` line matters for a base card; it passes when every stamp matches.

## Build a card

One command builds every card. The profile in `profiles/NAME.toml` holds everything that
differs between regions: the areas, the cells, the units and their DATA VERSION, what gets
written, and where the data files are. `[build] mode` picks the chain.

```
uv run build.py --profile NAME --base BASE-CARD.img --out OUT.img [options]
```

| Option | What |
|---|---|
| `--work DIR` | where generated files go (default `build/` and the name of `--out`); a rerun reuses the steps whose inputs did not change |
| `--osm FILE` | the OSM extract, instead of the profile's `pbf` |
| `--area A,B` | build only these `[areas.NAME]` tables |
| `--local FILE` | the local file (see below) |
| `--list` | print the steps |
| `--only STEP` | run one step |
| `--from STEP` | run from one step to the end |
| `--what TEXT` | the description for the card's row in the cards log |
| `--prev IMG` | an earlier card the check step compares against |
| `--stage DIR` | Iceland: where the POI steps keep their in-between images (default `WORK/stage`) |
| `--write GROUPS` | Iceland: what the cards step writes onto the card, overriding the profile's `[build] write` |
| `--map-from CARD` | Iceland: copy the map of a finished card with the same DATA VERSION onto this unit's base |
| `--restamp-from CARD` | Iceland: the same for a card with another DATA VERSION; every stamp is rewritten |

Each step prints the command it runs, so a step can be repeated by hand. The card is written
as `OUT.img.partial` and renamed to `OUT.img` only after the check step passes. A failed card
stays at `.partial`.

Paths and values that belong to one machine go in a local file, merged over the profile table
by table. The profile's `[build] local` names it (`local/NAME.toml`, ignored by git), and
`--local FILE` overrides that. Relative data paths are read from the repository root.

### Iceland (QY8, generate mode)

The card is built from scratch on the unit's base card: background, land use, roads with turn
restrictions, the coarse file with its graph, the building-scale map, addresses and POI icons
for every `[areas.NAME]` table.

1. Download `iceland-latest.osm.pbf` from download.geofabrik.de (Europe, Iceland).
2. Download the address register, `Stadfangaskra.csv`, from HMS (fasteignaskra.is, open
   data), and the postcode list from Pósturinn (postur.is) saved as `;`-separated CSV with
   the postcode in the first column and the town in the second.
3. Copy [examples/iceland.local.toml](examples/iceland.local.toml) to `local/iceland.toml`,
   or anywhere and pass `--local`, and fill in the three paths in `[data]`.
4. Build. `--out` must be `CARDS/builds/IS-NNN-UNIT-SLUG.img`: the unit (`ZE0`, `ZE1` or
   `ME0`) and the slug come from the name, `CARDS` is two directories up (made when missing),
   and the cards log there (`CARDS/CARDS.md`) gets a row once the card passes its check.

```
uv run build.py --profile iceland --base IS-000-ZE1-base.img \
    --out cards/builds/IS-001-ZE1-iceland.img --work work/ZE1
```

A full build takes 10 to 40 minutes; most of it is the building-scale map.

The three units share the map. Once one card is built, the other units' cards take a minute:

```
uv run build.py --profile iceland --base IS-000-ZE0-base.img \
    --out cards/builds/IS-001-ZE0-iceland.img --work work/ZE0 \
    --restamp-from cards/builds/IS-001-ZE1-iceland.img
```

`--restamp-from` rewrites the DATA VERSION, so it serves a unit on another database (ZE1 to
ZE0). `--map-from` copies the files as they are and refuses a source with another version
(ZE1 to ME0). Both keep the target's own software and settings.

The unit must also accept a car position west of 20 W first; see the shipment bound below.

### Finland (QY7, area mode)

The base card keeps its own map. The roads are stripped from every tile, and the roads of one
town are rebuilt from OSM. For a stock European Clarion QY7221NL card (2014 database) and
Geofabrik's `finland-latest.osm.pbf`. `profiles/finland.toml` names the town (`jyvaskyla`).

```
uv run build.py --profile finland --base STOCK-CARD.img \
    --osm finland-latest.osm.pbf --out NEW-CARD.img --work work/finland
```

`--area NAME` picks the town if the profile lists more than one. A second area is one more
`[areas.NAME]` table: an OSM relation, the one tile cell it sits in, and a bbox. The `[grid]`
cells are left out of the profile: build.py takes every tile on the base card that the
`[grid]` boxes touch.

Limits of the Finland card:

- Roads exist only inside the built area. Elsewhere in Europe the card draws the map but no
  roads, and cannot route.
- Address search still reads the factory data, which points at roads that are now gone. It is
  likely to crash.
- Guidance has not been tried on a unit.

## Try a card before the car

A card image can be booted in a QEMU-based emulator of the Clarion unit before it goes into a
car, with the unit's own NAND dump. That is the cheap way to see whether the unit accepts the
card and draws the map. Address search and the car position need a real GPS feed or a drive,
so the final test is on the unit.

## The 20 W shipment bound

The unit's firmware drops any GPS position outside its shipment area. On European units that
is longitude -20 to 68. Outside it, the map draws but the car never appears, and Adjust
Current Location fails too. Iceland lies west of -20, so an Iceland card needs the unit's
firmware patched: a table in the navigation program on the unit's NAND flash. Finland and the
rest of Europe lie inside the bound and need no patch.

The patch is a separate step, not in this repository. It needs a dump of the unit's NAND, the
table edited, and the dump written back.

## Profiles

One TOML per region in `profiles/`. `uv run tools/clarion_profile.py iceland` prints what the
tools will read. The keys are explained in comments in `profiles/iceland.toml` and
`profiles/finland.toml`.

## Limits

- Stand-alone Iceland cards are built from scratch, own tiles and own coarse file. Adding
  a country to a card that already carries others is untested; the coarse file is written
  fresh, not merged into what is there.
- Building outlines (25 m scale) are only drawn in populated areas: near a place node or
  residential, commercial, industrial or retail landuse. A house out in the open country
  gets no shape even where OSM has its footprint.
- North of about 60° the 640-160 km views look stretched east-west, as on factory cards.

## Docs

| File | What |
|---|---|
| [FORMAT.md](docs/FORMAT.md) | card files, tile grid, tile header, index |
| [GEOMETRY.md](docs/GEOMETRY.md) | tile records, section by section |
| [BUILD-LOG.md](docs/BUILD-LOG.md) | how the format was worked out: what the unit accepted and refused |
| [BUILD-STEPS.md](docs/BUILD-STEPS.md) | what each step of the Iceland chain writes, and the options behind it |
| [ROAD-CLASSES.md](docs/ROAD-CLASSES.md) | road widths and the OSM mapping |
| [LEVEL5.md](docs/LEVEL5.md) | the coarse levels |
| [ADDRESS-IMPORT.md](docs/ADDRESS-IMPORT.md) | addresses: the register reader and the shape another source needs |

## Licence

Code: GPL-3.0-or-later ([LICENSE](LICENSE)). Maps built from OSM: ODbL 1.0. Factory card
data: commercial, not covered. Details in [LICENSES.md](LICENSES.md); tests and what not to
commit in [CONTRIBUTING.md](CONTRIBUTING.md).
