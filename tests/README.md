# Tests

```
python3 -m unittest discover -s tests          # all files
python3 -m unittest discover -s tests -v       # case by case
```

## test_roundtrip.py

Needs no card, no OSM extract and nothing but the standard library. Every input is built in
code, so this is the one that runs on a fresh clone and the one to run while changing a tool.

It checks that decode(encode(x)) gives x back, and that the bytes in between match what
`docs/` records:

| what | against |
|---|---|
| the u32 address form, and 256-byte alignment | GEOMETRY.md |
| tile names to grid cells, and `tile_grid.py` | FORMAT.md |
| parcel codes at levels 1, 2 and 3 | the formulas in GEOMETRY.md and BUILD-LOG.md, over the whole database box |
| shape groups: polygon, polyline, point, sub-groups | GEOMETRY.md |
| background records: parse, build, byte-exact rebuild | GEOMETRY.md |
| a whole tile: `read_tile` then `write_tile` | byte-identical |
| longitude and latitude through a tile and back | within a parcel unit |
| road graphs: `rd_encode` then `clarion_rd` | nodes, links from both ends, class words, parcel edges |
| road shape records from `fa_encode` | GEOMETRY.md; nothing in `tools/` reads them back |
| the point flags, bit by bit, and the chains `rd_build` makes of links | the four-flag table in GEOMETRY.md |
| the OSM class mapping | the table in ROAD-CLASSES.md |
| land-use tags to kinds, kinds to area classes | `extract_landuse.RULES`, `clarion_bg.AREA_CLASSES` |
| `DRSTR001.DAT` from a tree of empty files | FORMAT.md |
| profiles | `profiles/iceland.toml` |

Seven cases need `shapely`, and skip without it. Six compare `bg_encode`, which cannot be
imported without shapely, against the two local copies of its record and directory builders
that the other cases use. If those drift apart, those six fail.

## test_format.py

The two map formats in `clarion_format`: X08-03-00 for QY8 units and X08-02-00 for QY7.
Headers in both formats rebuild through `tile_header.check`, the record codec gives `58 85`
or `78 9c` with the right padding, and a QY7 section 2 level 2 directory reads back with its
512-byte slot bodies. One case runs `tile_recode.py` on a factory QY7 tile and expects it
back byte for byte; it skips without one:

```
OSM2CLARION_QY7_TILE=B68R048R.DAT python3 -m unittest tests.test_format
```

## test_signposts.py

Section 4 signposts: `sec4_write.encode` against the record layout in SECTION4-draft.md, the
G words `fa_encode.shape_words` puts in a node record, a synthetic parcel with a ramp built
the way `rd_build.py --signposts` builds it and checked by `sec4_check.py`, and the
country-only record staying the 56-byte form. Three cases decode factory records and write
them back byte for byte; they need `OSM2CLARION_FACTORY_TILE` pointing at a tile with a
section 4 (`B48R060R.DAT`, 6,810 records) and skip without it.

## test_pois.py

Facility icons and POINT search records. The in-code cases check the position words (x
first), the mixed 8/10-byte layer-25 group with its side section and `clarion_bg.add_point`
(a dealer or camera point joining the icons), the slot-15 nearby tree, the SUB-CATEGORY entry
layout, the within-key record order, name collation (upper-cased, Þ and Ð sorted by their
folding), the factory's key cutting (`word_keys`), `extract_pois` tag rules and dedupe,
`poi_point.merge` on a POINT file built in the test (nothing added gives the file back; the
+0x3c hint guard trips on a third ref) and `poi_check` on that output, including a cleared
slot 279. Eight cases compare against factory data and skip without it:

```
OSM2CLARION_FACTORY_TILE=B48R060R.DAT OSM2CLARION_FACTORY_POINT=POINT018.DAT python3 -m unittest tests.test_pois
```

The tile is read one record at a time (never `read_tile`); POINT018.DAT is REFER001's
Luxembourg file, inflated. Checked there: every data and name record rebuilds byte for byte,
Luxembourg's towns land on their centres only with x read first, all 56 nearby category
blocks rebuild byte for byte, the whole slot-279 SUB-CATEGORY region (table, 42 tries, 42
record runs, 712,192 bytes) rebuilds byte for byte from the main keys, the same within-key
order gives 52 of the 56 CATEGORY record runs (main order 35), and the level-1 and level-2
records over Luxembourg Gare (178 and 553 points, 166 and 521 with side entries) parse and
rebuild byte for byte.

## test_reproduce.py

The claim in BUILD-LOG.md is that the chain reproduces a shipped database exactly: every
tile rebuilds byte-identically, every record rebuilds byte-exact, `make_mmain.py` reproduces
`MMAIN001.DAT` and `make_drstr_card.py` reproduces `DRSTR001.DAT`. Checking that needs the
shipped database, which is licensed commercial data. It cannot be
committed here, so these cases skip unless you supply a card:

```
OSM2CLARION_CARD=/Volumes/GEURX-30-105-100 python3 -m unittest discover -s tests
```

Mount the card read-only. An unpacked image works too; it needs `MAPAL001/` with the 52
tiles and `LEV3456R.DAT`, `MMAIN001.DAT`, `DRSTR001.DAT` and `VER_INFO/DB_INFO.TXT`. A full
tile reaches 477 MB and the whole surface is 4.8 GiB, so the rebuild
cases work through the smallest tiles until `READ_BUDGET` bytes have been read, 64 MiB by
default.

## test_card_check.py

`tools/card_check.py`'s ten rules against hand-built record dicts shaped like
`clarion_rd.parse_record`'s output, not real encoded bytes - enough to catch a rule
crashing or flipping its pass/fail sense. No card needed; every rule also passes
vacuously on an empty tile set, which is what a fresh clone exercises.

The card is identified from what it states about itself: the `DATA VERSION` and `NAVTEQ`
strings in a tile header, and `VER_INFO/DB_INFO.TXT`. Both databases are in
`fixtures/reproduce.json`.

### Digests

`fixtures/reproduce.json` holds the expected sizes and SHA-256 digests. Only hashes, byte
counts and the strings a card states about itself go in it; no card contents, ever.

It ships with the digests empty, because there was nothing to copy them from: BUILD-LOG.md
states the reproduction in prose and quotes no digest, and the files themselves cannot be
committed. Take them off a card once:

```
python3 tests/test_reproduce.py --record /path/to/card
```

That writes `MMAIN001.DAT`, `DRSTR001.DAT`, `LEV3456R.DAT` and all 52 tiles into the file
under the database the card reports. Commit the result and the comparison runs from then on.
Until it is filled in, that one case skips and the rebuild cases run anyway, since they
compare the card against itself rather than against a recorded digest.
