# Japanese cards: the DBDIR database

Status: **reader only.** Nothing in this fork writes a Japanese card yet. Step 1's gate
(decoded coastline on OSM within metres) passed on 24 Oct 2026; see "Drawing records" below. This page records
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
| Drawing record | header of 22 words, (u16, u16) offset pairs | section table: `u16 words`, then (u32 offset, u16 length) x 19 (drawing) or 14 (names) |
| Geometry group | `0001 0003 kind count` | `groups, 2*groups+1, kind\|count` then (offset, kind\|count) per further group; `0001 0003 8002` is the one-group case |
| Shape | count, type, ffff, x0, y0, int8 deltas, 0..2047 | length, id, scales\|n, type, attr, x0, y0, n deltas: int8 pairs, or int16 when attr & 0x100; 0..4096; zero deltas toggle drawn/undrawn edges |
| Parcel | 32x32 per directory, EU parcel code | BPCL level 1 = a quarter of a second mesh (1.875' x 1.25'), key low nibbles `0xc\|n`; BTPCL = the JIS 1/16 cell, key = mesh key |
| Datum | WGS84 | **Tokyo Datum** (Bessel); the three-parameter shift to WGS84 fits OSM |
| Roads, routing | section 2 of each tile | separate tables (`BLIA*`, 11.5 M rows; `BRGN*`) |
| Text | Latin, `1=NAME;2=REF` | Shift-JIS |
| REFER001/002, BICON001 | X08-03-00, `CLA-NAVI06-01` | X08-02-00, `CLA-NAVI05-01` (REFER002, BICON); REFER001 decompressed is an LE `H08-00-00` variant with longitude counted from 100E |
| `CMP.LST`, `.cmp`/`.ind`, `VER_INFO` line 2 `0100` | as FORMAT.md | same |

`tools/jp_dbdir.py` reads the container, the rows, the mesh keys, the blob framing and
the drawing records (`shapes`, `drawn_runs`, `parcel_geojson`).
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

### Drawing records (BPCL, BTPCLnn)

- **D row** (BPCLD 26 columns, BTPCLnnD 14): key `A, B` (u16 LE), a u16, then blob
  references: tag `0x29`/`0x2a`/`0x2b` = width of the LE length that follows (1-3 bytes), then
  u8 slot (1-based row in the page) and u24 LE page (1-based, in B-file pages). BTPCL rows end
  with two u32 columns. A B row with flags 0x4009 continues in the row its first four bytes
  locate.
- **Columns.** BPCL: 2 descriptor (corner as BE u32 lat, lon in 1/2048", then a scale word),
  3 neighbour key list, 4 a road record (section table of 61 words, not decoded), 5 the
  drawing record (19 sections), 6-8 name records (14 sections, Shift-JIS labels), 9-12 not
  decoded. BTPCL: 2 descriptor, 3 key list, 4 drawing record (zlib), 5 names.
- **Keys.** BPCL level 1: `A = deg<<7 | second-mesh column<<4 | 0xc | quarter`, B likewise in
  latitude; levels 2-7 put the level in A's low nibble and 0xf in B's. BTPCL keys are the plain
  mesh key (lon = 100 + A/128, lat = B/192). The corner in the descriptor matched the key on
  every row checked (a 1-in-10 sample of BPCLD: 6,908 level 1, 539 level 2; all 13,323 rows
  of BTPCL01/13/47).
- **Sections of the drawing record** (BPCL level 1; sea and rivers are measured, the other
  roles are first readings from type codes): 0 land cover polygons, 1 sea (0x0121) and
  inland water (0x0122/0x0123) polygons, 3 rivers and drains (0x0120 lines), 13 buildings,
  16 small area polygons (tail words), 18 points (three copies per point, one per scale bit).
  Polygons are cut into pieces along straight lines; cut and border edges are undrawn.
- **Census over the whole card**: BPCL 75,205 records, 4.84 M shapes, 1,525,978 of 1,525,978
  polygons closed, no coordinate outside 0..4096; 285 point sections (section 18) do not parse.
  BTPCL01-47: 160,148 records, 60.3 M shapes, 49,059,091 of 49,059,091 polygons closed, every
  section parsed.
- **Against OSM** (natural=coastline from Overpass, Tokyo -> WGS84 by the three-parameter
  shift): the gate parcel, BPCL `0bed 120f` (Hateruma), 162 drawn vertices, median 2.3 m, max
  15.3 m; the other way 399 OSM vertices, median 2.3 m, max 14.8 m. Shifted one parcel
  east/west/north/south: medians 327 m to 2.3 km. Six Hateruma parcels together: median 11 m.
  Ishigaki: median 15 m (OSM to decoded 5.8 m). Toshima (Izu): median 13 m, a generalised
  outline. BTPCL47 drains against OSM waterways in Ishigaki: median 6.5 m, shifted 190-390 m.
  GSI's TKY2JGD grid fits worse on Ishigaki (median 187 m), so the data sits on a uniform
  Tokyo Datum rather than the islands' local surveys.

## What a Japanese target needs, in order

Each step has a gate. Don't start the next step until the gate passes, the same discipline
that BUILD-LOG.md describes for the European tiles.

1. **Read every table.** Decode BALLDD (done), BPCL/BTPCL drawing records end to end (shapes,
   layers, types, names in Shift-JIS), the I-file index pages, and the control pages.
   *Gate:* one BPCL parcel's decoded coastline lands on OSM within metres, as GEOMETRY.md's
   check did. This also settles the 0..4096 extent and the shape encoding. **Passed** for the
   drawing records (above); names, roads (column 4), the index pages and control pages remain.
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
