# Japanese cards: the DBDIR database

Status: **reader only.** Nothing in this fork writes a Japanese card yet. This page records
what was measured and what a Japanese target needs, in the order it has to be built.

Measured on one card: volume `GJPNX-10-101-100`, database `15-02` (`SYSTEM_MAP_VER_NO`
20150401), card label 485-1972-00, from a 2015 Japanese-market Nissan LEAF. Offsets and
hexdumps behind each line below are kept with the card notes (not public yet).

## How it differs from the European cards

| | European QY8x (FORMAT.md, GEOMETRY.md) | Japanese card |
|---|---|---|
| Map surface | `MAPAL001/B..R..R.DAT` X08-03-00 tiles + `LEV3456R.DAT` | `DBDIR/`: 455 files, one B/D/I triplet per table of a paged RDBMS (Hitachi Entier, going by firmware strings) |
| Master, index | `MMAIN001.DAT`, `DRSTR001.DAT` | none. `DBDIR/BALLDD` is a key/value system table |
| Container | 4 KiB header, sector/block addresses, BE | `flhb` header, 4 KiB or 32 KiB pages, `ratm`/`raim`/`sbmb`/`sgmb` control pages, LE framing |
| Grid key | mesh `y = lat*1.5`, `x = lon + 924` | JIS X 0410: `A = (lon-100)*128`, `B = lat*192`, both u16. (B>>7, A>>7) is the first-mesh code |
| Drawing record | header of 22 words, (u16, u16) offset pairs | section table: `u16 words`, then (u32 offset, u16 length) x 19 or 14 |
| Geometry group | `0001 0003 kind count` | **same** |
| Shape | count, type, ffff, x0, y0, int8 deltas, 0..2047 | different: int16 deltas, extent apparently 0x1000 (not decoded) |
| Roads, routing | section 2 of each tile | separate tables (`BLIA*`, 11.5 M rows; `BRGN*`) |
| Text | Latin, `1=NAME;2=REF` | Shift-JIS |
| REFER001/002, BICON001 | X08-03-00, `CLA-NAVI06-01` | X08-02-00, `CLA-NAVI05-01` (REFER002, BICON); REFER001 decompressed is an LE `H08-00-00` variant with longitude counted from 100E |
| `CMP.LST`, `.cmp`/`.ind`, `VER_INFO` line 2 `0100` | as FORMAT.md | same |

`tools/jp_dbdir.py` reads the container, the rows, the mesh keys and the blob framing.
`tests/test_jp_dbdir.py` checks it on synthetic pages.

### Measured layout

- **File header** (`flhb`, offset 0): u16 file id, `01 55`, `flhb`, u16 name length, name;
  0x30 kind (`B` blob, `U` table/index); 0xde u16 page size (0x8000 on most B files, 0x1000
  otherwise); 0xe4 build stamp. The control pages are fixed: `ratm` at 0x1000, `raim` at 0x5000,
  `sbmb` at 0xc000, `sgmb` from 0xd000. Data begins at 0xe000.
- **Data pages** open with a u16 **table id** and `01 55`. The id is the same on every data
  page of a file and is shared by a table's B and D files (BPCLB and BPCLD both 0x02f9), so it is
  **not a checksum**. Candidate sums and CRCs were tested on four files and none matched.
- **Rows**: u16 length and u16 flags. Flags 0x4001/0x0001 are followed by `ncols` and an MSB-first
  presence bitmap of ceil(ncols/8) bytes. Flags 0x0000 (PADRN) have no bitmap. Blob rows use
  flags 0x0009, or 0x4009 when the row fills the page. Rows sit in 4 KiB pages even where the
  header says 0x8000.
- **Keys**: the leading two columns of every map table. Checked against prefecture extents
  (Hokkaido reaches 45.41N at Wakkanai and 145.6E at Nemuro; Tokyo spans 35.50-35.85N) and over
  all 11,479,311 BLIAD rows (122.98-145.86E, 24.07-45.56N).

## What a Japanese target needs, in order

Each step has a gate. Don't start the next step until the gate passes, the same discipline
that BUILD-LOG.md describes for the European tiles.

1. **Read every table.** Decode BALLDD (done), BPCL/BTPCL drawing records end to end (shapes,
   layers, types, names in Shift-JIS), the I-file index pages, and the control pages.
   *Gate:* one BPCL parcel's decoded coastline lands on OSM within metres, as GEOMETRY.md's
   check did. This also settles the 0..4096 extent and the shape encoding.
2. **Round trip.** Parse a whole D/B/I triplet into objects and write it back.
   *Gate:* byte-identical output for BALLDD, then a small table (BPCLR, BLELM), then BPCLD/I/B.
   This is where the allocation pages (`ratm`/`raim`/`sgmb`), the index order and the page
   fill factor get pinned down. The data pages leave about 0xd8 bytes free.
3. **Retarget a parcel inside Japan.** Replace one BPCL parcel with an OSM-derived one for the
   **same place** (coast, land, roads drawn), leaving routing untouched. This is the first card
   to try on hardware. Keep it inside Japan so the firmware's position bound can't be the cause
   of a failure. *Gate:* the unit draws it at the right scales on a **spare cloned card**.
4. **Roads and routing.** Decode BLIA/BRGN, then write links for a retargeted area.
5. **Anywhere else.** Southern latitudes have no key at all: `B = lat*192` is u16 and every
   stored value is positive (`jp_dbdir.mesh_key(174.78, -41.29)` raises). A grid shift in the
   style of `[shift]` would mean rekeying every table and index. Positions would still be
   refused unless the firmware's bound and its mesh conversion change, and the European unit
   showed the bound can't be moved from the card. This step depends on reversing the head
   unit's nav engine first (the G214 application image, compressed, not yet unpacked).

### What can be reused from the European chain

| reused | as is | notes |
|---|---|---|
| `extract_coast.py`, `osm_roads.py` | yes | OSM side, format-agnostic |
| `bg_tiles.clean_land`, simplification | yes | the land and island floors need re-measuring per level |
| `decompress_fast.py` | yes | the `.ind` count comes from the offset table, which suits the JP `.ind` (u32 count at 0x08) |
| `refer_header.py`, `icode_io.py`, `point_io.py` | no | X08-03-00 BE. JP REFER is X08-02-00 or H08 LE, and holds ETC/interchange data, not addresses |
| `clarion_tile.py`, `clarion_bg.py`, `fa_encode.py`, `rd_*`, `lev_*`, `make_drstr*`, `make_mmain.py` | no | X08 tile container, EU parcel code, EU shapes |
| `build.py` | the step model | needs a `--target jp-dbdir` with its own steps (`extract`, `parcels`, `index`) |
| profiles | the shape of them | a JP profile names JIS first meshes (e.g. `5339`) instead of `B..R..R` cells |

## Building and testing

- `python3 -m unittest discover -s tests` runs everything, JP tests included, with no card.
- Read a card through `tools/jp_dbdir.py summary|keys|blob`, with the card mounted read-only.
- Nothing here writes to `DBDIR/`, and nothing should until gate 2 passes. When a writer
  exists, build into `out/`, copy onto a spare card cloned from an image of the original,
  and keep the original card untouched.
