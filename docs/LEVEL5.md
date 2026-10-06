# Levels 4 and 5: the coarse views

`LEV3456R.DAT` holds whole-database directories for levels 3 to 6: one grid per level over the whole
map area, not a tile. Levels 4 and 5 are the two that get drawn at the wide zoom steps. Everything
below was read out of both shipped databases, the 2014 and the 2017, and matches what
`tools/lev_coarse.py` and `tools/lev5_roads.py` write.

## Which level draws which view

The zoom ladder runs 10 km, 16 km, 64 km, 160 km, 320 km, 640 km. 16 km draws section 1 level 4,
pair 0 of each column, the road record. **64 km draws section 1 level 5, the road block inside the
background record.** 160 km and wider draw no roads at all.

Established two ways. From the bytes: level 4 keeps roads and background in separate records, told
apart by w2, while a level-5 record is both at once and its second block parses as the eleven-pair
road layout with the shapes in layer 6. From the view: at 64 km an area whose level-5 records carry
no road block draws no roads, and draws its level-4 shapes at 16 km. Section 2 carries levels 5 and
4 as well, and is never drawn; it is the router's graph.

## Directory geometry, as measured

Header fields that describe the grid: 0x184 is the global bounds, four signed 16-bit arcminute
values (latmax, latmin, lonmin, lonmax), copied in part to 0x5d0; 0x806 is the section-2 table
address; 0x1000 is a word count then a group offset per level, level 4 at group 3 and level 5 at
group 2. 0x2ac is a `u16` section count, then per section a `(sid, nlevels)` byte pair, then per
level a 10-byte entry `level:u8, flag:u8, latmax:i16, latmin:i16, lonmin:i16, lonmax:i16` in
arcminutes. A section-table entry at group offset `o` is `w[o+2:o+8]` = `(y1, y0, x0, x1, z, t)`:
the box in mesh units, then an address in `(z<<16)|t`. Mesh units are 40' by 60', `y = lat * 1.5`
and `x = lon + 924`.

|  | rows x cols | parcel | pairs per column | slot body | typ | records |
|---|---|---|---|---|---|---|
| sec1 lvl5 | 11 x 11 | 8 x 8 meshes | 1 | 88 B | 0x90 | 121 |
| sec1 lvl4 | 44 x 44 | 2 x 2 meshes | 2 | 704 B | 0x91 | 1264 (2017 stock) |
| sec2 lvl5 | 11 x 11 | 8 x 8 meshes | 1 | 88 B | 0x90 | 42 |
| sec2 lvl4 | 44 x 44 | 2 x 2 meshes | 1 | 352 B | 0x91 | 513 |

A level-5 parcel is 8 deg of longitude by 16/3 deg of latitude, a level-4 parcel 2 deg by 4/3.
Rows never change under a westward widen, only columns move. A directory starts with six
big-endian words, `size/2, cols, rows, hy, hx, typ`, where `hy` is the south-west mesh row and `hx`
the south-west mesh column stored as `x0 - 1024`. Then one slot per row: a 4-byte address tag
(`0xffffffff` for an empty row) and `body` bytes of `u32` pairs, two per record slot. A pair is
`(compressed length / 2, plain length / 2)` on every level-5 record of both databases. Word 2 of the
plain record gives its kind:

| where | w2 | content |
|---|---|---|
| sec1 lvl5, parcel with shapes | 0x00fa | background block, optional road block |
| sec1 lvl5, empty parcel | 0x0fa0 | background block only |
| sec1 lvl4, pair 0 | 0x00fa | road shapes (530 records on one 2017 card) |
| sec1 lvl4, pair 1 | 0x03e8 | background (957 on the same card) |
| sec2 lvl4 | 0x0000 | routing graph |

## The level-5 record

22 big-endian words of header, then the sections they point at. A shipped section-1 level-5 record over
Ireland and west Scotland (row 6, col 2, lon -12..-4, lat 53.3..58.7, 71,516 bytes plain):

```
a80f c805 00fa ffff ffff 0000 0000 000b 1e52 1e5d 268a 44e7 00f0 ffff 0000 ffff ...
 w0   w1   w2   w3   w4   w5   w6   w7   w8   w9  w10  w11  w12
```

| words | content |
|---|---|
| w0, w1 | parcel code, absolute in a global 20' x 30' code grid (two code units per mesh) |
| w2 | record kind |
| w7, w8 | **road block**: offset and length, both in 4-byte units from the record start |
| w9, w10 | **background block**: same form |
| w11, w12 | names section |
| w13..w18, w20 | three unused section pointers, `ffff/0000` here; w20 is a per-parcel token |

The parcel code is absolute, so moving the grid west does not invalidate it: stock row 7 col 0
decodes `w0=0xac0f w1=0xc405` to mesh (88, 904), lat 58.67 / lon -20, and after a one-column shift
it sits at col 1, whose origin is the same mesh 904. A record with no road block has
`w7/w8 = ffff/0000`; 42 of the 121 records carry one, on each card.

## The layer table is (offset, shape count)

Read as `(offset, length)` the table does not close. On the record above, layer 6's pair is `0058/03c1`,
and 4 x 0x3c1 bytes from 0x18c ends at 0x1090 while layer 7 starts at 0x6dfc. Read as `(offset in
4-byte units from the block base, number of shapes)` every layer closes exactly. The block base is 0x2c
(11 dwords), the 11 pairs run to 0x58, and the data follows:

```
layer 0: start 0x58   count   8  -> ends 0x18c   (= layer 6's start)
layer 6: start 0x18c  count 961  -> ends 0x6dfa  (layer 7 starts 0x6dfc, 2 pad)
layer 7: start 0x6dfc count  78  -> ends 0x7972  (block ends 0x7974 = 0x2c + 4 * 0x1e52)
```

Shapes are self-describing, so a count is enough to walk a layer: word 0 of a shape is its length in
words including itself. The header's two pointer pairs are lengths in 4-byte units, the pairs inside a
block are shape counts, and a road block has 11 pairs against a background block's 26.

## Layer index is the class band, and the shape form

Over all 42 shipped level-5 road blocks of the 2017 database, 57,967 shapes. The top nibble of shape
word 2 is the class, and it tracks the layer:

| layer | shapes | classes |
|---|---|---|
| 0 | 137 | 10 (116), 11 (5), 12 (16), ferries |
| 6 | 42,476 | 3 (16,979), 4 (4,520), 5 (8,070), 6 (12,907) |
| 7 | 15,354 | 0 (9,475), 1 (2,304), 2 (3,575) |

The 2014 database bands the same way: classes 3 to 6 in layer 6 (5,639 / 17,614 / 2,225 / 14,053
shapes), classes 1 and 2 in layer 7, ferries in layer 0. Motorway through secondary belong in layer 6
at level 5, and use the same unnamed shape form as level-4 road records, which `lev_roads.shape_words_l4`
emits:

```
w0  length in words, including this one
w1  0x8000 | npoints
w2  class << 12
w3  0x3000
w4  name reference, ffff when unnamed
w5, w6  0
w7, w8  0x0800 | x, 0x4800 | y of the first point   (parcel units, 0..2047)
    then per further point: id, 0x2000|x, 0x4800|y   (0x3000|x, 0x4000|y on the last)
```

2,462 shipped level-5 layer-6 shapes carry exactly that signature (count flags 0x8000, w3 0x3000, name
ffff) on the 2017 database and 3,527 on the 2014 one, so the proven level-4 encoder is reusable
unchanged. The high bits on the y words vary in shipped data; 0x0800 on y means "not the last point".

## Budget: shapes and points per shipped level-5 road block

```
r8c5  lon 12..20  lat 64.0..69.3    196 shapes    811 points
r8c6  lon 20..28  lat 64.0..69.3    402 shapes  1,357 points
r6c2  lon-12..-4  lat 53.3..58.7  1,047 shapes  3,127 points
r7c4  lon  4..12  lat 58.7..64.0  1,853 shapes  5,094 points
```

Most shipped shapes are short: 38,071 have 2 points, 9,255 have 3, 4,242 have 4, one shape per link as at
level 4. `lev5_roads` caps a shape at 60 points, well above anything shipped. The Iceland build writes
518 shapes and 1,963 points in four records, which grow from 556/404/2,180/1,924 bytes to
2,888/892/13,564/5,892. That needs the joining step in `lev5_roads.chains`: the OSM extract splits one
trunk route into 1,683 ways, and one shape per way gave 2,998 shapes.

## There is no level-5 rendering defect on stock data

At 64 km this unit draws the coarse road layer with a wide red casing and a yellow core, far too heavy
for the scale. A factory 2017 card does the same over Scotland and Norway. An older note calling that
garbage geometry was wrong. Compared record by record, a westward widen altered no European record at
any coarse level:

- sec1 level 5: 121 records both sides, 108 byte-identical, 2 deliberately replaced, 11 dropped on the
  east, 11 new on the west.
- sec1 level 4: 1,262 identical, 2 replaced, 223 new, 0 dropped.
- sec2 level 5: 42 records, all byte-identical. sec2 level 4: 513, all byte-identical.

What the Iceland build was missing is the road block. Its rebuilt level-5 records had
`w7/w8 = ffff/0000`, so the 64 km view had nothing to draw, though the same area's level-4 road
records drew normally at 16 km. `lev5_roads.py` adds the block without rebuilding the background: it
takes the record apart at its own offsets, puts the road block in front of the background and
renumbers w7 to w12, so background and names come through byte for byte.

## Which slot holds the background

On the 2017 database all 121 section-1 level-5 records keep the background at w9/w10, shipped and
rebuilt alike. A rebuild on the 2014 database put it at w7/w8 with `w9/w10 = 0000/0000`, because that
build picked the slot from w2 and w2 is 0x00fa on any level-5 parcel with shapes. The geometry blocks
are byte-identical between the two cards (468, 316, 2,072, 1,800 and 256 bytes); only the pointer slot
differs. The unit then reads the coastline and sea polygons as road shapes: at 64 km the island comes
out a solid fill hatched with thousands of thin red lines, cut off dead straight at the parcel edge.
16 km on the same card is unaffected.

A tool must pick the slot by inspection rather than assume w9/w10. `clarion_bg.bg_slot` is the one
rule, and `lev5_roads.bg_slot` calls it: the background is at w9/w10 when w9 is neither 0 nor 0xffff
and w10 is non-zero, at w7/w8 otherwise, where `lev5_roads` moves it to w9/w10 before the road block
goes in. An offset of 0 cannot be real, because the 44-byte header puts the first block at unit 11.

Counted over the level-5 directory of three files: neither shipped card carries the `0000/0000` form
(2017, 121 records: 18 background-only, 42 with both blocks, 61 empty; 2014, 121 records: 10, 42, 69),
while a 2014 rebuild carries it in 13 of its 121.

## Two bugs worth not repeating

The per-level box table at 0x2ac. `lev_coarse.py` used to rewrite a bound only when it already equalled
the matching global bound at 0x184, so inner edges never moved under `--shift`. On a shifted 2017 card
that left sec1 lvl4 lonmax at 2760 (should be 2280), sec1 lvl3 lonmax at 2700 (2220), and sec2 lvl4 at
-1080..2760 (should be -1560..2280). Drawing is unaffected. A box describing the unshifted grid
can only make the unit miss data west of its lonmin, never misplace data inside it. The live
consequence is on section 2 level 4, whose lonmin stays at -1080 arcminutes, so the router's
coarse graph has nothing west of 18 W. Every bound now moves by the same delta, and the six are
12 bytes in the header if a built card needs a direct patch.

Picking the geometry slot from w2. `clarion_bg.parse_record` used `gi = 7 if w[2] == 0xfa else 9`.
w2 is 0x00fa both on a level-4 road-only record, whose geometry is at w7/w8, and on a level-5 background
record, whose geometry is at w9/w10. The rule now reads the slot off the pointers, and `parse_record`
hands it to `build_record` so a round trip keeps it. Parse-then-build over every section-1 level 5, 4
and 3 record of both databases: level 5 went from 110 byte-exact / 132 mismatched to 158 / 84, level 4
unchanged at 1,708 / 1,043, level 3 unchanged at 18,808 / 13,194. The 84 that still differ are the 42
road-carrying records in each database, whose road block `clarion_bg` does not model.

That replacement rule tested `w[9] == 0xffff` alone, so it read a record of the 2014 rebuilt kind
(`w9/w10 = 0000/0000`) at w9/w10 and raised on a layer table taken from offset 0: all 13 such records
in a 2014 rebuild failed, level 5 coming out 66 byte-exact / 42 mismatched / 13 errors. The rule is now
`clarion_bg.bg_slot`, which counts both `ffff/0000` and `0000/0000` as an empty pair, and which
`lev5_roads.bg_slot` calls so the two cannot drift. Those 13 records now round-trip byte for byte at
w7/w8, for 79 / 42 / 0, and the shipped-byte counts above are unchanged.

## Not known

- w20. All 42 shipped level-5 records with a road block set a non-ffff w20 and all those without set
  ffff, so something reads it. Its meaning is unmeasured. The Iceland build writes 0x0318, the token
  shipped level-4 pairs use, and the roads draw.
- The 0x7fffffff pair sentinel. `lev_coarse.read_dir` skips `0xffffffff` only; the sentinel occurs in no
  directory of either database, so the gap is latent.
- Coarse labels. The 2017 database carries level-5 names sections of 44, 44, 64 and 80 bytes, label type
  0x4089 flags 0x2800 for a sea label and 0x0086/0x8086 for land ones. The 2014 database has
  `w11/w12 = ffff/0000` on the same records, no names section at all.
