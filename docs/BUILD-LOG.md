# Building a region onto a base card: what the unit accepted

A measured record of adding one region to a shipped European database and getting a Clarion QY8x
head unit to draw it. Every result below came off a real unit, most of them a 2017 one, over ten
builds between 23 and 26 September 2026. The failures pinned down more of the container's rules than
the successes did, so they are here too.

The region was four cells of the `MAPAL001` grid plus the matching parcels at levels 3 to 6 in
`LEV3456R.DAT`, built from an OSM coastline extract by `tools/extract_coast.py` and
`tools/bg_tiles.py`: sea polygons over the whole of each cell, coastline lines at levels 1 and 2, a
sea text label. Decoded back against the OSM input, level 2 lands at a median 0.5 m and level 1 at
0.13 m in both directions, and every tile rebuilds byte-identically.

## How the unit finds a tile

Two cards, same four new tiles, different index:

| index | result |
|---|---|
| regenerated, all names sorted | boots, whole map black, Europe included |
| 53 original entries in original order, new names appended after `LEV3456R.DAT` | Europe draws, the new region is ignored |

So the unit addresses tile files by position in the `MAPAL001` index list, and the positions come
from the 52 cells of the level-3 group in `LEV3456R.DAT`. The two lists are not in the same order
(checked 25 Sep 2026). The index is sorted by name; the group is sorted by (ymin, xmin). Because `x`
falls as the R number in a cell name rises, name order runs east to west where the group runs west
to east.

Keep each list in its own order rather than making them agree. Both shipped orders were reproduced
and the unit draws. A cell that is not in the level-3 group has no position, so its tile is never
opened, and a name inserted ahead of existing ones shifts every later tile onto the wrong file. That
is the black map in row one above. For a build: append the new cells to the level-3 group with
`tools/lev_append.py`, sort the index with `tools/make_drstr_card.py` (its `--append` keeps the
base's own order instead, as a control), and keep the `MMAIN001.DAT` totals in step.

`MMAIN001.DAT` is the database master: 4 KiB, the tile header block, the database bounds at 0x184
and 0x5d0, and a level table at 0x1ac whose counts are the sums of every tile's level table plus
`LEV3456R.DAT`. The 2017 Europe base reads 52 level-3, 170 level-2 and 2,246 level-1 directories.
`tools/make_mmain.py` reproduces the shipped file byte for byte from the 52 tiles plus
`LEV3456R.DAT`, which is how the totals rule was confirmed. Files other than the tiles that carry
the bounds constant: `MMAIN001.DAT`, `LEV3456R.DAT`, `REFER002/*`, `TFTBL001/TFCAT253.DAT`,
`RDSTM001/*`.

## The black map, and the bug behind it

The first build shipped tiles only: `LEV3456R.DAT` original, section 2 empty, `MMAIN001.DAT` still
the Europe one, so the declared map edge was still -20 degrees. The unit booted and drew black at
every zoom. Two things were wrong at once. First, the coarse levels: the default view wants level 3
or coarser and only `LEV3456R.DAT` provides those, so a card with new tiles and an untouched coarse
file has nothing to draw at the zoom it opens at. The third build added level 3. Still black.

Second, the parcel codes. Comparing a shipped open-sea record with a built one byte for byte found
it: the built records carried the parcel code of the directory-local row and column (`000c 000c`)
instead of the absolute one (`ac0c cb6c`), so every parcel claimed to sit near the equator. The code
function itself was right, checked against 5,403 tile records and 15,232 level-3 records. The
encoder passed it the wrong numbers and the round trip never checked codes.
`bg_encode.build_parcel_record` now takes the directory mesh, and both the round trip and the tile
build verify every record's code. Two corrections went in alongside: sea polygons are forced
clockwise, which every shipped sea polygon is, and every parcel inside coverage gets a record. The
first build skipped parcels that were entirely land, where the shipped data gives them a header-only
record with an empty name section.

The useful diagnostic is the 640 km view. The Europe base paints grey west of -20 and draws Norway;
the broken card painted the whole screen in the land colour and drew nothing at all, not even
Europe's untouched levels 4 to 6. That is a global failure, not a tile one: the unit read the new
bounds, then rendered no polygon from any file. At 25 m over a city a plain beige screen is also
what correct data gives, when the build has no roads and no buildings.

## Level 3 in LEV3456R.DAT

Level-3 directories are one per tile cell. Header `0826 0010 0010 y x 0091`: 16 slots of 20
arcminutes with 256-byte bodies of 32 pairs, so 16 columns of 30 arcminutes. Records use type prefix
`0xa0xx` (`0xa012` sea, `0xa007` coast) and this parcel code, checked against all 15,232 shipped
level-3 records:

```
row3 = 2*y_mesh + row, col3 = 2*x_mesh + col
w0 = 0x8000 | (row3>>2)<<8 | (row3&2)<<6 | (row3&1)<<4 | 0xf
w1 = ((col3>>2)&0xff)<<8 | (col3&2)<<6 | (col3&1)<<4 | 0x3
```

`lev_append.py` appends the new level-3 directories and their blocks at the end of the shipped file,
extends the level-3 group of the section table (it is last in the table and fits before the first
directory at 0x1300), bumps the level-3 count and sets the bounds. Every original byte stays as
shipped. The appended coastline decodes back at a median 2 m from OSM. Shipped level-3 records with
no labels omit the name section (`w11 = ffff`), so the encoder does the same at every level. Levels
4 to 6 are single whole-database directories with slot bodies of 704, 176 and 16 bytes.

## What the container refuses

- The level-3 group must stay sorted by (row, column) as cells are added. Appended out of order,
  they are not found.
- Zooming below 64 km is only allowed where the whole-database level-4 directory has a parcel (2 by
  2 meshes). `lev_coarse.py` adds them.
- Shapes with more than about 600 vertices are not filled. `bg_encode.to_parcel` simplifies to a
  600-vertex budget; the shipped maximum is 677.
- A shape group holds at most 255 shapes. In a fjord dense with islets, level 2 drops the smallest
  of a parcel: 104 of 40,476 sampled OSM vertices end up more than 20 m from a decoded line, worst
  case 156 m. Level 1 is unaffected.
- Levels 6, 5 and 4 each need their section-table box, their directory header and the 0x5cc and
  0x184 values to agree. Bounds at -28 with the level-6 box still at 904 killed level 6 outright,
  blank at 640 km, until the box and the record were widened to match.
- The declared extent has to contain everything on the card. One build set the bounds to the union
  of Europe and the new region (lat 21.33 to 80, lon -28 to 68) in the tiles, `LEV3456R.DAT` and
  `MMAIN001.DAT` so the untouched levels 4 to 6 stayed inside it.

The record at 0x5cc is `8000 0000`, then the south-west corner (latmin, lonmin in arcminutes, the
copy at 0x5d0), then two bytes that are (11, 11) on the shipped 88 by 58.7 degree database: the grid
size in 8-mesh blocks. The unit reads that pair and derives positions from it. Changing it to (11,
12) moved the unit's own reported position from Szczecin into the Atlantic; (12, 11) moved it to
France. Which byte is columns was never proven, because the build that works leaves the pair alone.

## The five cards that proved a widened directory is refused

The goal was reaching -28 degrees instead of -20, and the first approach widened the coarse
directories westward. `lev_coarse.py` grew the level-4 and level-5 directories by whole 8-mesh
blocks, filled the new columns from OSM, relocated the directories and every row block to the end of
the file, then updated the section-table boxes, the bounds and the per-level box table at 0x2ac.
`lev_l6.py` widened the level-6 box, rescaled every shape of its single raw record into the wider
box, added a sea polygon for the new strip with the region cut out, and fixed the directory header,
slot and box.

Europe tiles drew fine from 4 km down on all five. Above that:

| card | change | result |
|---|---|---|
| 1 | bounds -28, level 4/5 widened, grid pair (11, 11) | 640 km to 10 km blank |
| 2 | same, pair (11, 12) | blank, position jumped to sea |
| 3 | same, pair (12, 11) | blank, position jumped to France |
| 4 | level 4/5 widened, v2 header everywhere | 640 to 160 km fine, 64 km to 8 km blank |
| 5 | all levels consistent at -28, level 6 widened too | 640 to 160 km fine, whole region drawn; 64 km to 8 km blank; 4 km and below fine, region included |

The widened level-4 directory (48 columns) and level-5 directory (12 columns) are what the unit
refuses, under either header version: it assumes 44 and 11 columns and never reads the count.
Reading the widened file back also forced `clarion_bg.parse_group` to handle layers with several
sub-groups: header `nsub, header-words`, then per sub-group a kind byte, a count byte and, for all
but the last, the word offset where it ends. The plain `0001 0003` header is the one-sub-group case.
Level-5 records use kind 0x00fa when they carry shapes and 0x0fa0 only when empty; level 4 is 0x03e8
throughout. Levels 1 to 3 have 0x0019 in that word, so it is a scale code.

## The shifted grid

The fix is to keep every directory at the size it ships and move the grid 8 degrees west instead.
Level 5 stays 11 columns, level 4 stays 44, and the database runs -28 to 60 east, bounds
`4800,1280,-1680,3600`. The 8 degrees dropped off the east held no level-4 records and eleven
level-5 sea parcels over Kazakhstan. `lev_coarse.py --shift` and `lev_l6.py --shift` do it; the
level-6 record is shifted and clipped, not rescaled.

Cards 6 to 10 drew Europe and the whole of the new region at every zoom from 640 km to 200 m. The
grid pair at 0x5d4 stays (11, 11), so its byte order never mattered. Level 5 (the 64 km view) and
level 4 still have to be rebuilt for the new cells even where shipped parcels exist, because the
shipped ones are sea only. `lev_coarse.py` replaces them.

That replacement is untested over a populated cell. Only four cells have ever been built, the
four over Iceland, and they were the easy case: their level-4 and level-5 parcels shipped
empty or sea-only, so there was nothing to preserve. `lev_coarse.py` replaces a coarse parcel
outright, and nothing has checked what that does where the shipped parcel already holds a
populated European cell. Expect to lose whatever was there. The level-3 and tile levels are
built fresh per cell and should be fine.

## The shipment bound

A unit only accepts positions inside the shipment area baked into its firmware, which for a European
unit is longitude -20 to 68. Build a map outside that and the unit draws it correctly, then refuses
every position in silence: the coastline is right, the roads are right, the vehicle never appears,
and nothing logs an error. Nothing on the card changes this. The card's own declared bounds have no
bearing on it, which is worth knowing before spending a day patching them. Iceland sits west of -20,
so all four cells built here are outside the area.

## What draws and what does not

The shipped map shows skerries only when zoomed in, so small islands are dropped per level.
`bg_tiles.clean_land` drops islands below an area floor and draws the coast from the remaining
outline; `lev_append.py` (level 3), `lev_coarse.py` and `lev_l6.py` use the same function. The
floors are 0.001 deg<sup>2</sup> at levels 6 and 5, 0.0002 at level 4, 0.00003 at level 3.

The level-6 sheet is plain degrees and the unit draws it as such, so a northern region looks about
twice too wide at 640 to 160 km. Norway does the same on the shipped map. Nothing to fix in the
data.

Level-5 names sections hold road-number labels (`2=R147`, type 0x00ff, flags 0x2000). Layer 25 of a
level-5 record carries city points of type 0x800c with no text in the record, so city names at 64 km
come from somewhere other than the record. Untested: a sea-style text label (type 0x4089, flags
0x2800, as the tiles use) in a level-6 or level-5 names section. The 100 m and 50 m views first
measured as drawing nothing over the new region; that stopped being true once section 2 was built,
and they draw roads with names (5 Oct 2026).

## Country badges, layer 25 of the level-6 record

Country labels are points in layer 25 of the level-6 record, and the point type is the country.
`lev_l6.py --points` places them.

| code |  | code |  | code |  | code |  |
|---|---|---|---|---|---|---|---|
| 0x8096 | GBR | 0x8097 | FRA | 0x8098 | DEU | 0x8099 | ITA |
| 0x809a | ESP | 0x809b | SWE | 0x809c | ALB | 0x809d | AND |
| 0x809e | BEL | 0x809f | BIH | 0x80a0 | BGR | 0x80a1 | DNK |
| 0x80a2 | EST | 0x80a3 | FIN | 0x80a4 | GRC | 0x80a5 | IRL |
| 0x80a6 | HRV | 0x80a7 | LVA | 0x80a8 | LIE | 0x80a9 | LTU |
| 0x80aa | LUX | 0x80ab | MKD | 0x80ac | MDA | 0x80ad | MNE |
| 0x80ae | NLD | 0x80af | NOR | 0x80b0 | AUT | 0x80b1 | POL |
| 0x80b2 | PRT | 0x80b3 | ROU | 0x80b4 | CHE | 0x80b5 | SRB |
| 0x80b6 | SVK | 0x80b7 | SVN | 0x80b8 | CZE | 0x80b9 | TUR |
| 0x80ba | UKR | 0x80bb | HUN | 0x80bc | BLR | 0x80bd | RUS |

`0x8040` to `0x807f` are POI icons: parking, airport, hotel, dealer and so on. There is no badge to
be had for a country outside the table. The label text is in no card file, searched; it comes from
the unit's firmware. Probes of `0x8080` to `0x8095` and `0x80be` to `0x8130` drew nothing.

## The two database vintages

The 2014 and 2017 vintages have the same coarse-file structure. Same bounds (4800, 1280, -1200,
4080), same grid pair (11, 11), level-6 directory at 0x1300 kind 0x000a, level 5 at 0x0200 11x11,
level 4 at 0x3cde 44x44, 52 level-3 cells at 0x0826 16x16, and the level-3 group last in the section
table with room before 0x1300. The same 52 grid cells. Every assertion in `lev_append.py`,
`lev_coarse.py` and `lev_l6.py` holds on both, so the whole chain ports between vintages with only
the tile header template swapped. Rebuilt on the 2014 base, the four new tiles come out
byte-identical to the 2017 build past the 0x1000 header, with 6 header bytes differing: the data
version string. The 2014 base's own tiles pass the same checks as the 2017 ones, byte-identical rebuild,
2,635 records byte-exact and every parcel code correct, and its tile header differs from the 2017
one only in the version strings.

The index differs in one respect: the 2014 base does not hoist `VER_INFO` to the front of its
directory, the 2017 base does, and neither matches readdir order. So `make_drstr_card.py` takes
the order from the base's own index rather than deriving it: given `-` as the tile folder it
reproduces that index byte for byte, which is the control for any index change. `make_drstr.py`
builds from readdir order instead and carries the `VER_INFO` hoist.

## Rules that held across every build

- Append new cells to the level-3 group, and keep that group sorted by (row, column).
- Sort the `MAPAL001` index by name, and never insert a name ahead of an existing one without
  rebuilding the level-3 group to match.
- Keep `MMAIN001.DAT` totals equal to the sum of every tile's level table plus `LEV3456R.DAT`.
- Give every parcel inside coverage a record. Land-only parcels get a header-only record with an
  empty name section.
- Omit the name section when a record has no labels (`w11 = ffff`). Wind sea polygons clockwise.
  Simplify to a 600-vertex budget per shape.
- Never change a directory's column count. Move the grid instead.
- Keep the section-table box, the directory header, 0x184 and 0x5cc in agreement, per level.
- Verify every record's parcel code against its directory mesh, not against local row and column.
- Leave every byte you did not have to change exactly as shipped.

Related: `docs/FORMAT.md`, `docs/GEOMETRY.md`, `docs/LEVEL5.md`, `docs/ROAD-CLASSES.md`.
