# Tests

```
python3 -m unittest discover -s tests          # both files
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

## test_reproduce.py

The claim in BUILD-LOG.md is that the chain reproduces a shipped database exactly: every
tile rebuilds byte-identically, every record rebuilds byte-exact, `make_mmain.py` reproduces
`MMAIN001.DAT` and `make_drstr_card.py` reproduces `DRSTR001.DAT`. Checking that needs the
shipped database, and the shipped database is licensed commercial data. It cannot be
committed here, so these cases skip unless you supply a card:

```
OSM2CLARION_CARD=/Volumes/GEURX-30-105-100 python3 -m unittest discover -s tests
```

Mount the card read-only. An unpacked image works too; it needs `MAPAL001/` with the 52
tiles and `LEV3456R.DAT`, `MMAIN001.DAT`, `DRSTR001.DAT` and `VER_INFO/DB_INFO.TXT`. The card
is only read. A full tile reaches 477 MB and the whole surface is 4.8 GiB, so the rebuild
cases work through the smallest tiles until `READ_BUDGET` bytes have been read, 64 MiB by
default.

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
