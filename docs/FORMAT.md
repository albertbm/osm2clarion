# The card and its files

What a Clarion QY8x map card holds, and in what layout. Figures come from two European base
cards: the 2014 database, volume `GEURX-30-100-100`, database `14-38`, tiles at data version
`14/11/17/01`; and the 2017 database, `GEURX-30-105-100`, `17-33`, `17/09/01/01`. Tiles of both
declare format version `X08-03-00` and the structures below hold on both, though the sizes
differ. Every figure is from the 2017 card unless the line says otherwise. The `.cmp`/`.ind`
container is dfj's, from his gist "Clarion Maps structure + decompressor of CMP files"; for
what is inside a tile see [GEOMETRY.md](GEOMETRY.md), as this file stops at the tile header.
The earlier QY7 is a different platform, SH-4 with a monolithic map and no `MAPAL001` grid,
and its data is not this format.

## Card layout

One vfat filesystem, about 13.8 GB of it. Sizes as measured:

| path | size | what it is |
|---|---|---|
| `MAPAL001/` | 4.8 GB | 52 map tiles plus `LEV3456R.DAT`. The map surface. Not compressed. |
| `REFER001/` | 3.4 GB | 199 address and postcode files as `.cmp` + `.ind` pairs |
| `REFER002/` | 9.5 MB | `ICODE001.DAT`, the place-name index |
| `DRSTR001.DAT` | 467 KB | the directory index the unit reads instead of scanning the card |
| `CMP.LST` | | NUL-separated list of the compressed files |
| `VER_INFO/DB_INFO.TXT` | | line 2 must read `0100` |

Also present, none of it needed to draw a map: `BICON001`, `DFRMR001`, `HOUSE001`,
`MMAIN001.DAT`, `OBJCT001`, `OPNG`, `RDSTM001`, `SPEECH`, `TFTBL001`, `TMC`, `XPICT001`. Nothing
is encrypted: entropy on a map tile measures about 3.99 bits of a possible 8, structured data in
the clear, not a cipher and not deflate.

## The two halves do not share a format

`CMP.LST` holds 199 entries, all under `REFER001`; nothing else on the card is listed. By
prefix:

```
42 ADDRE   addresses            38 ZCODE   postcodes
42 INTER   intersections        33 FRWAY   freeways
42 POINT   points of interest    1 COORD     1 ZIPFR
```

`MAPAL001` has no `.cmp` or `.ind` at all, so the map surface ships unwrapped and the container
reaches address lookup and postcodes only. Nothing in the build chain needs the
decompressor: `tools/decompress_fast.py` is here to read a base card's address half when a
question about one comes up, and that is all. dfj's original slices the remaining buffer once
per block. That is quadratic, and on a 300 MB file of about 4,600 blocks it never finishes.
This one slices to the next offset and writes one output file per input.

## The map surface

52 tiles plus `LEV3456R.DAT`, 4.81 GiB in total, largest tile `B48R060R.DAT` at 477 MB. Every
tile opens with the same UTF-16BE magic, `0043 004c 0041 0052 0049 004f 004e` for `CLARION`,
then 0xFF padding to the next field. `LEV3456R.DAT` sits outside the grid; the B in `BLEV...`
listings is the grid prefix, not part of its name, and the name suggests zoom levels 3 to 6,
still a guess.

### The grid is hexadecimal

Tiles are named `B{nn}R{nnn}R.DAT`, and the odd 2/8 alternation in those numbers is an
artefact of reading hex as decimal: `28 30 38 40 48` are `0x28 0x30 0x38 0x40 0x48` = 40,
48, 56, 64, 72, plain steps of 8.

```
lat_min = B_hex * 2/3 degrees     cell height 5.333 deg  (320 arc-minutes)
lon_max = (108 - R_hex) degrees   cell width  8 deg      (480 arc-minutes)
```

Longitude decreases as R increases. Each tile declares its own box in its header, and on all 52
grid tiles that box lies inside the cell the formula predicts, as `tools/tile_grid.py` checks. A
mostly-sea tile declares a box clipped to where it has data, so the declared box is often
smaller than the cell. The cell is what the name encodes.

| tile | cell | declared box | size |
|---|---|---|---|
| `B48R060R` | 48.0-53.3N, 4-12E | same | 477 MB, the densest tile |
| `B30R060R` | 32.0-37.3N, 4-12E | 36.5-37.3N, 11.6-12.0E | 245 KB, Sicily channel, mostly sea |

Run it the other way for the cells a region needs. Iceland, 63.4 to 66.6N and -24.5 to -13.5W:

| filename | cell | covers |
|---|---|---|
| `B58R078R.DAT` | 58.67-64.0N, -20 to -12W | south-east |
| `B58R080R.DAT` | 58.67-64.0N, -28 to -20W | south-west |
| `B60R078R.DAT` | 64.0-69.33N, -20 to -12W | north-east |
| `B60R080R.DAT` | 64.0-69.33N, -28 to -20W | north-west |

Neither card has them: the grid runs B28 to B68 and R038 to R078, so -20 to -28W is missing.

### Tile header

Fixed-width UTF-16BE fields padded with `0xFF`, then two index tables.

```
0x000  CLARION                   0x0a0  FORMAT VERSION X08-03-00
0x040  ZENRIN                    0x0e0  DATA VERSION 17/09/01/01
0x080  CLA-NAVI06-01             0x162  V06.00.00
0x184  12c0 0500 fb50 0ff0       database-wide constant, identical on every tile
0x18c  32 bytes of '?'           unset name field
0x1ac  level table
0x2ac  bounding-box table        offset varies; scan past the 0xFF run from 0x1e0
```

Both tables open with a uint16 section count, then per section a uint8 id and a uint8 entry
count followed by that section's entries. Level table entries are `level, type, uint16 count`,
levels descending 06 to 01; the counts track tile density, 1 for `B30R038R` at 261 KB against 60
for `B58R060R`, so they are per-level sub-block counts and not offsets. Bounding-box entries are
`level, 00, int16 lat_max, lat_min, lon_min, lon_max`, in arc-minutes.

## The compression container

For reading the address half, nothing else. `CMP.LST` is a NUL-separated list of paths in
Windows form with a `\SystemSD\` prefix to strip; a single entry means a directory, whose
`.ind`/`.cmp` are paired by basename, and with no `CMP.LST` the fallback is `REFER001/`. Each
`.ind` is the index for its `.cmp`:

```
0x00  uint32 LE   uncompressed size
0x04  uint32 LE   compressed size
0x08  uint8       block count
0x09  7 bytes     metadata, unidentified
0x10  uint32 LE[] one offset per zlib block, running to the end of the file
```

Every offset points at the start of a zlib stream in the `.cmp`. A block ends where the next
starts, the last at the end of the file; decompress each and concatenate. The byte at `0x08`
matches the number of offsets on small files, but it cannot hold the roughly 4,600 blocks of a
300 MB file, so take the block count from the length of the offset table. Checked on
`REFER001/ADDRE001.DAT`: 29 blocks, each 64 KiB uncompressed, rebuilding to 1,870,080 bytes,
exactly the size in the header. Across all of `REFER001`, 3.37 GiB of `.cmp` becomes 8.85 GiB.

A decompressed file opens with the same `CLARION` magic as a raw tile, so the compression is
a wrapper and nothing more. Its header carries the provenance, all UTF-16BE. On the 2017 card:
`CLARION`, `ZENRIN`, `CLA-NAVI06-01`, `FORMAT VERSION X08-03-00`, `DATA VERSION 17/09/01/01`,
`V06.00.00`, `NAVTEQ 17Q2`. So the underlying map is NAVTEQ 2017 Q2, now HERE, packaged by
Zenrin for Clarion; the 2014 card is 14Q2, which its tile headers carry at 0x03c1 alongside
the data version. It is licensed commercial data.

`REFER001` is four search indexes and a postcode set: address by street, intersection by street
pair, POI by administrative area, motorway junctions, postcodes. Each decompressed file ends
with a schema block of length-prefixed ASCII names, uint16 length first, so the data names its
own fields: `06 00 "STREET"` for the table, `09 00 "area code"` and `0b 00 "street name"` for
its columns. `area code` appears in every family and is presumably the join key to the
administrative hierarchy. The records between header and schema are packed binary, not decoded.

## DRSTR001.DAT, the directory index

The unit reads this instead of scanning the card, so a database with new tiles needs a
matching index or the tiles are invisible. `tools/make_drstr.py` regenerates it from a
directory tree. Against the 2017 base card the output is byte-identical to that card's own
`DRSTR001.DAT`, all 466,952 bytes, and so it is against a second 2017 card (`GEURX-30-105-100`,
`17-31`). The 2014 base card is 469,192 bytes and does not come out identical: its `VER_INFO`
sits in a different place, as the ordering note below sets out, so 11,167 bytes differ.
`make_drstr_card.py` reproduces it instead.

```
directory records, 0x28 bytes each, from offset 0, one per directory
    0x00  char[20]   name, NUL-padded
    0x14  uint16 LE  own id, 1-based, in record order
    0x16  uint16 LE  parent id. Tree linkage, not a type. The root's parent is itself.
    0x18  uint16 LE  child count
    0x1a  10 bytes   zero
    0x24  uint32 LE  absolute offset of this directory's child block

child blocks follow the record array in record order, 0x20 bytes each
    0x00  char[20]   name
    0x14  uint16 LE  zero
    0x16  uint16 LE  2 = directory, 0 = file
    0x18  uint32 LE  zero
    0x1c  uint32 LE  size in bytes
```

dfj's notes read `0x16` of a directory record as a type. It is the parent's id: the `00` to
`16` directories carry 7, which is `OBJCT001`; `MKR_OPNG` carries 8, which is `OPNG`; the
nested `02` carries 32, which is `MKR_OPNG`. The 2017 card has 33 directory records, so the
array runs `0x000`-`0x527` and the first child block starts at `0x528`. dfj's example has it
at `0x578`, so read the offset rather than hardcoding it.

Three rules are not guessable from a hex dump. Each was found by failing to reproduce the file.

- Compressed files are listed logically. The card holds `ADDRE001.DAT.cmp` and
  `ADDRE001.DAT.ind`, and the index carries one `ADDRE001.DAT` entry whose size is the
  uncompressed 1,870,080, read from the `.ind` header. The `.ind` files are not listed, so
  `REFER001` shows 200 children against 399 files on disk.
- Directory sizes sum what is listed, not what is on disk, then truncate to 32 bits.
  `REFER001` reads 1,251,633,152 against 3,953,262,803 actual bytes; `MAPAL001` reads
  867,939,328 for a directory really holding 5,162,906,624.
- Order is on-disk FAT order, not alphabetical. `OBJCT001` lists `OBJCTIDX.DAT` before its
  subdirectories, exactly as readdir returns it. Compared directory by directory against
  five card images, the index order is readdir order in every directory of all five.

The one entry that does not line up is `VER_INFO` in the root, and that is a fact about the
cards, not about the format. Its directory entry was deleted and written again after the
index was made, so its slot today is not the slot the index was built from. The entries that
were deleted carry the dates: the 2017 card's index was written 2017-10-23 14:46, when
`VER_INFO` was the first root entry, and it moved to the last on 2017-10-30. Hoisting it back
to the front reproduces that card. The 2014 card's index was written 2014-12-19 11:52, when
`VER_INFO` sat one slot ahead of `TFTBL001`; putting it back there reproduces all 469,192
bytes, and hoisting it to the front does not. A third card, `GEURX-30-105-100` / `17-31`,
never moved its `VER_INFO` and needs no exception at all.

Two independent dumps of the same 2017 card, with different sha256, give the same answer, so
this is not a difference between an original card and a clone. It is a difference between a
directory that has been written to since mastering and one that has not.

On the one North American card measured (`GNAMX-20-107-100`, `18-02`, 88,984 bytes) the order
matches in all 19 directories, and 34 bytes differ, all inside the `SXM` subtree: the size
fields of `SXM` and `SXM/AL` read 46,448 and 0 against sums of 3,730,375 and 3,683,927, five
20-character `.psv` names are cut to 19 characters with a zero size, and one entry the index
spells `ltntbl.dat` has only an 8.3 `LTNTBL.DAT` on disk.

`SPEECH` and `DRSTR001.DAT` are absent from the index, and have to be: `SPEECH` holds
33-character filenames that will not fit the 20-byte name field, and a file cannot record its own
size, because writing the size would change it. `make_drstr_card.py` shares the code but takes
the order from an existing index rather than from readdir, for a card whose `MAPAL001` has been
replaced.

## Not established

- The 7 metadata bytes at `0x09` of each `.ind`.
- The record schemas inside a decompressed `REFER001` file; the container yields bytes, not
  records. `ZCODE` has no schema block in the tail window; `COORD` and `ZIPFR` are unexamined.
- `REFER002/ICODE001.DAT`, the place-name index, and what `LEV3456R.DAT` holds beyond the guess
  in its name.
- Whether the unit validates the database beyond `DB_INFO.TXT` line 2.
