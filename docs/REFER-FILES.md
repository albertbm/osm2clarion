# REFER001 files this repository does not write: COORD, ZIPFR, FRWAY, ZCODE form B, ICADM

Measured on two 2011 QY7 cards, read from their images on 24 Nov 2026: the European
`QSD-309-101` (image SHA-256 `105e2032…76307`) and the North American `QSD-301-103`
(`53342430…c5383`). Both declare `FORMAT VERSION X08-02-00` and `DATA VERSION` 11/06/27/01 and
10/08/12/01. [FORMAT.md](FORMAT.md) lists `COORD`, `ZIPFR` and `FRWAY` as unexamined and has
nothing on how `ZCODE` files differ from each other. Nothing here was measured on a QY8 card,
and none of it has a reader in this repository's tools: the tools that decode these files are
in [OSM2Leaf](https://github.com/neutrinus/OSM2Leaf) (`coord_zcode_icode.py`, `zcode_decode.py`,
`frway_decode.py`, `zipfr_decode.py`, `icadm_decode.py`), and each prints its counts.

Every file below is the decompressed `.DAT`; see [FORMAT.md](FORMAT.md) for the `.cmp`/`.ind`
container. All words are big-endian.

## Files per family on the two cards

| family | EU 2011 | US 2011 |
|---|---|---|
| `ADDRE`, `INTER`, `POINT`, `ICADM` | 33 each | 11 each |
| `ZCODE` | 33 | none |
| `FRWAY` | 26 (001, 006, 013, 014, 017, 024 and 032 missing) | 11 |
| `COORD001`, `ICODE001` | 1 each | 1 each |
| `ZIPFR001` | 1 | none |

A factory card does not carry a file for every region of its table. A card built from OSM
that adds a region should not assume one.

## COORD001: a table of region boxes

`REFER001/COORD001.DAT`, 4,608 bytes on the EU card and 4,352 on the US card.

```
0x1000  u32 guard                 EU 0xf0, US 0x56; equal to the last record's level word
0x1004  u16 count                 EU 33, US 11
0x1006  u16 7                     both cards
0x1008  u32 0x00008000            both cards
0x100c  u32 0
0x1010  count x 14-byte records   then one all-zero record, then zeros to the end
```

A record is `u16 x0, y0, x1, y1` and a 48-bit mask kept as `u32 high, u16 low`:

```
lat = (x - 32768) / 192
lon = (y - 52742) / 128
```

Measured: record 21 on the EU card (counting from 1) decodes to lat 49.06 to 54.90, lon 14.07
to 24.17, which is Poland's box; record 1 decodes to lat 42.48 to 42.66, lon 1.43 to 1.80 (Andorra). On the US card record 1 decodes
to lat 32.57 to 42.06, lon -124.44 to -114.06. The box of record 33 on the EU card runs from
lat 36.15 to 60.90 and lon -8.20 to 1.80, which is the United Kingdom's with Gibraltar.

The mask of every record but the last is one bit, `0x400000000000 >> (i - 1)` for record
`i` (EU 32 of 32 and US 10 of 10; on the EU card record 32's bit sits in the low word, `0x8000`).
The last record's mask is an aggregate, `0xf00000` on the EU card and `0x560000` on the US
card, and its high word equals the guard at `0x1000`.

Not established: what reads this file. In our searches of the 2011 navigation program no
name reference to it was found and it was not read while the unit mounted the card, so whether
the unit uses it at all is open. The longitude origin
`52742` is not the `52736` the tile stream keys use; the six units of difference are as measured
and unexplained.

## ZIPFR001: 14-byte keys with a sector and a mask

EU only, 520,448 bytes. Every byte is accounted for:

```
0x0000  512-byte common header
0x0200  0xe00 bytes of the shared template
0x1000  u16 3, u16 projection_end, u16 swap16(projection_end), u16 7
0x1008  36,881 records of 14 bytes, then 10 zero bytes
```

Record: `u8 flag, u8 0, char key[6], u16 0, u16 group, u16 mask`. `flag` is 0 on 36,857 records
and `0x80` on 23, the letters `A` to `Z` expanded for prefix lookups; one more record, at
index 36,877, is a marker (`80 00 00 03 f0 64`, then zeros) and is the `projection_end` of the
body header. Keys are digits, upper-case letters and spaces, zero padded (lengths 1 to 6;
31,450 have five characters). `key[4:6]` takes 119 distinct values, `group` 131 and `mask`
118. Not established: what a key, group and mask stand for, and what reads the file.

## ZCODE: two layouts in one family

[ADDRESS-IMPORT.md](ADDRESS-IMPORT.md) and `zcode_io.py` write one layout, the one with the
header at `0x1000` and the trie at `0x1100`. On the EU card only two of the 33 files have it,
`ZCODE018` (the Netherlands) and `ZCODE033` (the United Kingdom). The other 31 have a second
layout:

- `0x1000` holds a stub (`u32` at `0x1006` is 0 and the record address at `0x1012` is
  `0xffffffff`). The real header is repeated at `0x1100`, with the same fields at the same
  offsets plus `0x100`: node width at `0x1106`, record address at `0x1112`, record size at
  `0x1116`.
- The 16-byte records sit where the record address says, in the sector encoding of
  [FORMAT.md](FORMAT.md): `base = (enc >> 8) * 0x800 + (enc & 0xff) * 0x100`, and the file
  holds `record size / 8` of them. `ZCODE002` has the address `0x1007`, so base `0x8700`, and
  record size `0x57c0`, so 2,808 records.
- The tree is a table of 12-byte entries from `0x1204`: `u16 size, u16 0, u8 tag, u8 0,
  u16 count, u32 offset`, with the tag a digit or letter. The target of an entry is at
  `0x1204 + offset * 2`: another table, or `count` records.

A record in this layout is eight halfwords with `0xffff` for a level not reached and `0000` last;
the first record of `ZCODE002` is `(1, 0, 0, 1, 0, 65535, 65535, 0)`. OSM2Leaf's
`tools/zcode_decode.py census` on the EU card walks all 31 files of this layout without an
invalid target; the part of each file it does not reach is 0 to 32 percent, which is padding and
gaps between tables. Whether a unit accepts a postcode file written in the first layout in place
of one of these 31 was not tried.

## FRWAY: route numbers and the names under them

Present on both cards. A shard is, in order:

- the 512-byte common header and a type block at `0x1000` of 0x108 bytes whose words
  `ffff` at `+0`, `0201` at `+4`, `4d40` at `+0x10` and `0601` at `+0x32` hold in every one of
  the 37 shards of the two cards;
- a table of 12-byte records from `0x1110`: `u16 key, u16 0, u8 id, u8 0, u16 length, u16 0,
  u16 offset`, ended by key `ffff`; behind it a directory of name initials (`u8 code, u8 0,
  u16 count, u16 block, u16 offset, u16 length, u16 tail`);
- zlib streams, each with a `4e 20 00 00` and a `u32` in front: one index stream, one stream of
  route records (`A1`, `E70`, `E773`, tagged `0xa0`) and one to 25 main streams. The schema in
  each shard reads `area code`, `route number`.

Counted on the EU card: 101 table records, 94,040 main records and 1,322 route records in 26
shards; 2,908,874 of 2,921,728 bytes are accounted for (99.56 percent), the rest being 14 bytes,
one per shard at the table terminator + 0x0d, each a multiple of 6. Not established: what the
fields of a main record stand for.

## ICADM and ICODE001: the area tier labels

Both files start the same way after the common header: at `0x1000` a `u16` table size in
halfwords, a `u16` country count, then one 462-byte entry per country: `u16 231`, `u16` ISO 3166
numeric code, seven 64-byte tier-label slots (Latin-1, NUL padded) and ten `0xff` bytes. The
EU card has 33 entries (15,250 bytes) and the US card 2 (928 bytes, Canada first). The first
entry of `ICADM001` on the EU card is ISO 20 (Andorra) with the labels `2=COUNTRY`,
`2=PARROÒUIA` (Latin-1, the `Ò` as one byte) and `2=SETTLEMENT`; on the US card ISO 124
with `3=COUNTRY`, `3=PROVINCE`, `3=COUNTY`, `3=CITY` and `3=SETTLEMENT`. The label's digit
differs between the two cards. The table is the same in `ICODE001`; its name tree, which
follows in chains of rows, is not described here.
