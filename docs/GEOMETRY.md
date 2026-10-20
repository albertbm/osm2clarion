# Tile geometry

No documentation of this format exists. Everything here was measured, first on the four
smallest tiles of a European card (`B30R060R`, `B30R038R`, `B58R070R`, `B28R070R`) and then
checked against the OSM coastline for the same ground. Dates in parentheses are when a thing
was established. For the card around the tiles, see [FORMAT.md](FORMAT.md).

The payload is not a varint stream, which an earlier reading of it assumed. It is a directory
tree of zlib records.

## Measured against OSM

| check | result |
|---|---|
| Decoded coastline against OSM, level 2 (7.5′ x 5′ parcels) | 1,806 vertices, median 6 m, max 29 m |
| Decoded coastline against OSM, level 1 (1.875′ x 1.25′ parcels) | 1,827 vertices, median 2-5 m, max 52 m |
| Same decode with the parcel shifted one column | median 8.7 km |
| Rebuild every zlib record from its parsed form | byte-exact, 4 tiles, 6,630 records |
| Rebuild the whole tile from its parsed form | byte-identical, all 4 tiles |
| Encode the OSM GeoJSON -> tile -> decode -> compare | level 2 max 5.0 m, level 1 max 1.3 m, both directions |

The clipping check holds: every decoded parcel stops at 12.0°E.

## What the tile is

The format is a KIWI descendant (the Japanese car-navigation map standard Zenrin co-wrote):
first-level meshes of 1° x 40′, 2048-byte sectors, parcels with normalised coordinates.

Mesh coordinates: `y = lat_deg * 1.5`, `x = lon_deg + 924`. A tile `BbbRrrr` is the 8x8 mesh
block with `y0 = bb_hex`, `x0 = 1024 - rrr_hex`. That is the same grid `tile_grid.py` checks,
restated in mesh units.

```
0x000   header strings, 0x184 db bounds, 0x1ac level table, 0x2ac bbox table (FORMAT.md)
0x4ee   0001 0110 0000 0120     constant in all 52 tiles
0x5cc   8000 0000 lat_min lon_min 0b0b    db bounds again
0x800   section table: entries of u32 address + u16 size, terminated by ffffffff
0x1000  section 1 table, then directories and record blocks, all 256-byte aligned
```

Addresses are u32: `(offset // 2048) << 8 | (offset % 2048) // 256`. The u16 after each
address in the section table is the section table's own size in 256-byte units.

### Section tables

Section 1 is the map surface (background polygons, coastlines, names). Section 2 is the road
network (undecoded, see below). Large tiles carry a section 4 (level type 0x93), also
undecoded. The level table at `0x1ac` lists, per section, `level, type, count`; the count is
the number of directories at that level.

Each section table:

```
u16 nwords
u16 x nlevels   word offset of that level's group, ffff if none (same order as the level table)
per group:  u16 len (= 2 + 6*count), u16 count, then count x {ymax ymin xmin xmax  u32 address}
```

Box values are mesh coordinates. Level 2 directories cover a 4x4-mesh quadrant, level 1
directories a single mesh.

### Directories

```
u16 0x2046 (section 1) or 0x1046 (section 2)   u16 0x20  u16 0x20  u16 y  u16 x  u16 type
32 slots of: u32 address + body (512 bytes for 0x2046, 256 for 0x1046)
```

A slot is a **parcel row**. The body is an array of u32 pairs `(compressed words,
inflated words)`; the pair index is `2*col + 1` for the background record of parcel `col`
in section 1 (index `2*col` is a second record kind, present only where ferry routes cross),
and `col` in section 2. Absent pairs are `ffffffff ffffffff`, except even pairs in level-1
section-1 directories which are `7fffffff 7fffffff`. Empty slots have address `ffffffff` and
the same fill.

The address points at the parcel row's block: the records of that row, concatenated in
pair order, zero-padded to 256 bytes. So one block holds every record of one parcel row.

Parcel grid: 32 rows x 32 columns per directory, row 0 at the south, column 0 at the west.

| level | directory covers | parcel size | 2048 units = |
|---|---|---|---|
| 2 | 4x4 meshes | 7.5′ x 5′ | 16,384 x 24,576 units per degree |
| 1 | 1 mesh | 1.875′ x 1.25′ | 65,536 x 98,304 units per degree |

### Records

Every record in these tiles starts with `58 85`: a zlib stream (window 8 KiB, level 6;
`zlib.compressobj(6, DEFLATED, 13)` reproduces the shipped bytes). Odd-length streams get
one zero byte. The inflated size is the second number of the slot pair, in words. Section 2
level 2 records are the exception: stored raw, two parts of `a` and `b` words.

Inflated background record, all big-endian, "unit" = 4 bytes:

```
w0 w1      parcel code, see below
w2         0x0019 = background record (0x00fa = the ferry-route record kind)
w3 w4      ffff ffff at level 2, 347a 2aef at level 1 (constant across tiles, meaning unknown)
w5 w6      0
w7..w20    seven (offset, length) pairs in units from the record start:
           (w9,w10) geometry, (w11,w12) names, (w15,w16) point index; unused = ffff 0000;
           (w19,w20) is 0000 ffff
w21        0
```

Parcel code, with `row`/`col` absolute in that level's parcel units (level 2:
`row = y_mesh*8 + slot`, `col = x_mesh*8 + col`; level 1: *32):

```
level 2:  w0 = 0x8000 | (row>>4)<<8 | (row&15)<<4 | 0xf      w1 = ((col>>4)&0xff)<<8 | (col&15)<<4 | 2
level 1:  w0 = 0x8000 | (row>>6)<<8 | (row&63)>>2<<4 | 0xc | (row&3)
          w1 = ((col>>6)&0xff)<<8 | (col&63)>>2<<4 | 0xc | (col&3)
```

(`tools/clarion_bg.py: parcel_code`, verified on 5,403 records in four tiles.)

Geometry section (at unit 11): a layer table of `(offset, length)` unit pairs relative to
the section start, `ffff 0000` when empty; the table's own length equals the first offset
(26 entries in every record seen). Then the groups:

```
group:  0001 0003, u8 kind (0x80 polygon, 0x40 polyline, 0x00 point), u8 shape count,
        shapes, zero-padded to 4 bytes
shape:  u16 vertex count, u16 type, u16 ffff, u16 x0, u16 y0, (count-1) x (int8 dx, int8 dy)
point:  u16 type, u16 ffff, u16 x, u16 y
```

Polygons repeat the first vertex last. Long straight edges are walked in +/-127 steps. A
parcel that is all sea holds one rectangle `(0,0)->(0,2047)->(2047,2047)->(2047,0)`. A parcel
that is all land has no layer 1 at all.

Layers and types seen (level 2 uses `0x50xx`, level 1 the same low byte with `0xc0xx`):

| layer | kind | type | what |
|---|---|---|---|
| 0 | polygon | 0x502b | built-up area |
| 1 | polygon | 0x5012 | **sea**, clipped to the parcel |
| 4, 5, 7, 9 | polygon | 0x5026 0x5074 0x5015 0x5011 | other land cover, plus one group format (`0002 0005`) not parsed |
| 11 | polyline | 0x5007 | **coastline**; 0x500a/0x500b other lines |
| 25 | point | 0x100c 0x400c ... | place markers |

Names section: `u16 6, u16 count` then five `ffff 0000` pairs, then entries
`u16 type, u16 flags, u16 x, u16 y, u16 len, text` (0x4089/0x8089 area labels such as
MAR MEDITERRANEO; 0x4068 point labels carry an extra u16 id before the length;
0x00ff 0x2000 entries are road and ferry names, `1=name;2=ref` style). Level-1 records with
roads carry further sub-blocks here that are not decoded.

### Section 2

Road data, decoded 24 Sep 2026 on a 43-record control tile: all records parse to their own
counts and every link is seen from both ends. `tools/clarion_rd.py` reads it and
`tools/rd_encode.py` writes it.

Directory: kind `0x1046`, `ab` (32, 32), `y`, `x` mesh, type `0x90` at level 1 and `0x81` at
level 2, 32 slots of parcel rows, 256-byte bodies of one `(compressed words, inflated words)`
pair per column; level-1 records are zlib, level-2 records raw in two parts. The section
table lists levels 5, 4, 2, 1; the level table at 0x1ac and the box table at 0x2ac carry a
section-2 group (box in arc-minutes of the data extent).

Record: words 0-1 parcel code (same as the background record), two zero dwords, five
`(u32 offset, u32 length)` unit pairs in 16-bit words from the record start, then from word
26 the ISO 3166 numeric country code of every country in the parcel, one word each (578
Norway; `0242 02f0` where the Norway-Sweden border crosses), up to where unit 0 starts:
word 27 for one country, 28 for two. Unit 0 is the graph: `u32 nodes, u32 link ends`, then
eleven `(u16 nodes, u16 link ends)` pairs at u4..u25, then the nodes from u26. A node counts
in the pair of its own class: classes 0-2 at u6, 3-7 at u8, 8 at u10, 9 at u12, 10 and up
(ferries) at u24; u4 and u14..u23 stay zero. Its link ends are its entries less any
parcel-edge entry, and the totals are the sums, so edge entries are never counted.
Measured 18 Oct 2026 by `tools/rd_check.py` on 183,567 records of 14 tiles from three cards
(214-062, 214-066, 218-120), both levels, with no exception; 115 records in the two densest
tiles do not parse yet. Unit 2, in about 4% of records, is `0102 9800 ff00 0018 d801 ff00 0018`.

Node, in ascending x: `u16 x` with the entry count in the top nibble and the parcel
coordinate in the low 11 bits, `u16 y` with the node's class in the top nibble (the lowest
link class among its entries) and the coordinate in the low 11 bits, a flag word (0x2000 on a
level-1 node that also exists at level 2: 4,822 of 4,822 flagged nodes in three tiles have a
level-2 twin, 98 of 44,662 unflagged ones do),
then per entry `other node index, w4, w5, link id, class, a, b` and
`((w5 >> 6) & 0x3f) + (w5 & 0xf)` extra words. The class word is the far node's class in the
high byte and the link's class word in the low byte (`0x0598` is a class-8 link reaching a
class-5 node). The link id carries `0x2000` at the end with the larger y. `a` is the travel
time and `b` the length (`rd_encode.cost_words`). Extra words look like turn or lane lists
(`1001 2001 2002 0001`, `f001`); w4 is the same at both ends of a link (0, 0x80, 0x2, 0x140
...), and w5's top bits `0x8000`/`0x4000` at opposite ends of 5% of level-2 links look like
one-way marks. A node on a parcel edge has a last entry whose class word is the side in the
top bits (0x8 top, 0xa right, 0xc bottom, 0xe left), the node's own class, then the low byte
of its link's class word, and whose "other node" is
an index in the neighbouring parcel. Curvature is shape nodes of degree 2; there is no
separate geometry, and the background records carry no road lines.

**Drawn road geometry is a separate record**: the even pair of a level-1 background
directory (`2*col`, kind `0x00fa`, earlier mistaken for ferry routes) exists for exactly the
parcels that have a road graph (37 of 37 on Pantelleria, 42,077 of 45,154 in the Paris tile).
`tools/fa_encode.py` writes it. Header: parcel code, `0x00fa`, `347a 2aef`, 0, 0, then
`(w7, w8)` = geometry unit at dword 11 (length in dwords), `(w17, w18)` = a 5-dword tail
(`0004 0001 ffff 0000 x3 NN01 0000`, NN = shapes + links), w19 `0x8104`, w20 `0x017c`. The
geometry unit: u16 (unknown count), twelve `(offset in dwords from the unit start, shape
count)` layer pairs, a pad word, the shapes. Layer 5 holds roads, layer 0 ferries. A shape
is a chain of consecutive link ids along one way: `u16 length in words (itself included)`,
`0x8000 | points` (sometimes with 0x4000), `0x8000`, `0004 000c 8004` (or `0144 ffff 8004`),
`0`, the start node `(0x0800 | x, 0x2800 | y)`, then per link `style, value, link id` (style
0x1031 roads, 0x001e ferry; value unknown; from the second link only `value, id` unless the
style changes, see below), the link's
points `(0x1000 | x, y)` with the first point of a link flagged 0x7000 (first link) or 0x5000,
the link's end node with `0x0800 | y` unless it ends the shape, and one trailer word. Points
are absolute parcel coordinates. Curves are the shape points; the graph only has the ends.
On a unit (24 Sep 2026): one street drew from 200 m to 50 m with one shape per link in the
single-link form (start node, `1031 0100 id`, end point flagged 0x7000, trailer 0). A chained
form that put first-point and link-end flags on the same point drew lines off the parcel.

The style word is the posted speed limit (18 Oct 2026, factory B58R060R Norway and B50R068R
northern England, 0x2046 directories only). Bit 5 set means a limit, `((w & 0x1f) + 1) * 5` in
the country's unit; low bits `0x1e` with bit 5 clear mean none. Bit 12 is set on most words and
clear on most ferries and one-way links (20 Oct 2026, B58R060R and B50R060R: clear on 34,118 of
43,845 one-way links and 6,330 of 1,521,536 two-way ones); bit 6 is unknown and toggles on its
own along a chain. The shape's point order says nothing about the direction: 22,495 one-way
links are drawn with the traffic and 21,350 against it. The direction lives in section 2: w5 bit
0x4000 on the entry at the end traffic leaves, 0x8000 where it arrives (`rd_encode.oneway_bits`). A y with `0x2000`
is followed by a style word, so a chain whose limit changes sets `0x2800` on the y of the link
end before the change and opens the next link with `style, value, id`. Norway has 28,643 such
restyles and northern England 6,868; every one differs from the word before it, and 20,169 and
3,302 of them change the limit, so a limit change needs no new shape.

The x word carries four flags, not three:

| bit | meaning |
|---|---|
| `0x4000` | first point of a link |
| `0x2000` | in the first link |
| `0x1000` | the link goes on past this point |
| `0x0800` | this point does not end a link |

A link end inside the chain clears `0x1000` and carries `0x0800` on its y instead; the
shape's last point keeps `0x1000` and a plain y. Counted over every level-1 shape of the
Tromso tile `B68R058R.DAT`, 7,520 shapes, `0x0800` is set on 7,520 start, 7,373 first-of-link
and 15,797 inner points, and clear on 5,818 link ends and 7,520 last points, with no
exceptions. The German tile `B48R060R.DAT`, 1,344,744 shapes, agrees to within 0.1%, and
every exception is in a shape with extra header words that the walker does not fully decode.

`0x0800` is easy to miss. The other three flags are the top nibble, `x >> 12`, and a census
taken at that shift never sees bit 11. The firmware reads a clear bit as "this link ends
here", not as "the link continues", so a build that left it clear on inner and first-of-link
points ended every link at its first point and had the two words after it read as a link
header. That card drew a fan of crossing lines below 500 m and the matcher stopped finding
the road. `0x0c00` on a link end's y was an earlier reading of the same mark; bit 10 belongs
to the coordinate, so that form moved the point 1,024 units and drew off the parcel.
Two-point single-link shapes carry neither bit, which is why one shape per link always drew.

Level 2, read off a dense city tile: graph records ship raw in two parts with
class `0x0797` and header tail `0x00fa`; part b is a connection table `nodes, 0xa100`, per
node `2000 k 0 k` then per entry `(other, side flags, other)` (0x00c0 left, 0xc000 bottom
seen), then the parcel code and 0. The level-2 shape record has no count word: eleven
layer pairs from word 22, roads in layer 6, data at dword 11 from the unit start (word 44),
header words 3/4 `ffff ffff`, w20 `0x00fa`, no tail unit; shapes are `len, 0x8000|points,
0x7000, 0, name offset, 0, 0, start (x | 0x0800), 0x101e, id, points` with no value word
and no trailer; unnamed shapes (0xffff) are allowed. Level-2 names items are `8000 0009
80xx 0006 0bc0 00fa 0001 k name-offset` with k 1 for a named shape (0 unnamed), one per
shape. A build with the pairs one word late hung the unit at 2 km. `rd_build.py` writes
both levels; which OSM classes reach level 2 is a profile setting, and
[ROAD-CLASSES.md](ROAD-CLASSES.md) has the measurements behind the default. Level-2 roads
with names drew from 1 km down, and did not hang.

Coarse roads live in `LEV3456R.DAT`: section 2 has a level-5 directory (11x11, one pair per
column, 42 raw two-part records, classes 0x393/0x515/0x696...) and a level-4 one (44x44,
one pair per column, 513 records); the level-4 background directory's even pairs hold 513
shape records (kind 0x00fa, level-2 layout with eleven pairs, header word 20 = a per-parcel
token that the graph record's header tail repeats, shapes `len, 0x8000|points, 0xa000, 0,
name, 0, 0, start (0x0800|x, 0x0800|y), id, points` with the end point flagged 0x3000, no
style word). Level 5 has no shape pairs. `lev_roads.py` moves both road directories west
with the grid and writes level-4 shape records for the main roads, with no graph records.
Level 5 is in [LEVEL5.md](LEVEL5.md).

**Level 3** (2 and 4 km): the 52 shipped level-3 cells hold 6522 shape records in their
even pairs, level-2 layout (eleven pairs, roads in layer 6, header word 20 a per-cell
token), shapes `len, 0x4000|points, class<<12, 0, name, 0, 0, points` with no link id and
the start x flagged 0x0800. `lev_roads.py` writes them for the cells being built (motorway to
tertiary, classes 3 to 7) as one polyline per way run; two-point shapes drew as dashes with
gaps. `lev_compact.py` rewrites the coarse file keeping only referenced pieces.

**Road class.** Word 2 of a shape carries the class in its top nibble: 8 local streets
(every level-1 shape), 7 collectors (every level-2 shape), 6 down to 2 for the main road
classes at level 4, 0xa ferries. The graph records use the same digit in their class word
(`0x898`, `0x797`, `0x696`/`0x686`, `0x595`, `0x393`, `0xa8a`). The unit chooses classes per
zoom: level-4 shapes written as class 0xa drew blue (border style) at 16 and 10 km and not
at 4 and 2 km; level-2 class-7 shapes draw from 1 km down only. Unnamed level-4 shapes have
word 3 `0x3000` and 0x4000 on their y words; named ones word 3 `0` and 0x0800/0.
The street name shows in the status bar once the name entry has its 10-byte header and the
shapes point at it. `rd_build.py` builds a whole cell with graphs, shapes and names. A point
becomes a graph node only where ways meet or end; the bends between nodes ride inside the
link, and a chain runs along a way and across OSM way boundaries wherever class, name and ref
agree, as the factory's chains do. `--no-chain` goes back to a node at every point and two
points per shape, which gives the matcher one disconnected fragment per bend. Measured against
the position a car reports in its own probe data, that form held a median +0.25 m from the
road centreline over 2,817 samples where chained shapes held +0.08 m over 209. House numbers
and address search are still open.

Road names sit in the background record's names section as `00ff 2000 0000 0000 len
"1=NAME;2=REF"` entries (10-byte header, no id word; the text is padded to an even length
and the length includes the pad). A shape's header word 4 is the word offset of its name
entry from the start of the names section (12 for an entry right after the header), 0xffff
for no name; word 3 is 0x0004 or 0x0144. The second sub-table (pair 2 of the names header,
`0008 count ffff 0000 x3`) has one item per shape in shape order, `8000 0009 80xx 0006 0bc0
017c 000e 0008 000d` with `80xx = 0x8000 | 2*(points - 2)`.

### LEV3456R.DAT

Same container. Section 1 holds levels 6, 5 and 4 as one directory each covering the whole
database extent (y 32-120, x 904-992), and level 3 as 52 directories, one per tile cell. A
region drawn at the coarse zooms needs entries there too, and the shipped extent stops at
-20°. Reaching past it means moving the whole grid west by whole 8-mesh blocks, because a
widened directory is refused: the unit assumes 44 level-4 and 11 level-5 columns and never
reads the count. The Azores and Svalbard need the same move as Iceland. See
[BUILD-LOG.md](BUILD-LOG.md) for what the unit accepts and what it refuses.

## Tools

All in `tools/`, Python 3. `shapely` is needed wherever geometry is built; `osmium` only by
the two OSM readers.

| tool | what it does |
|---|---|
| `clarion_tile.py` | read a tile into objects and write it back. Run on a tile it checks a byte-identical rebuild |
| `clarion_bg.py` | parse and build inflated background records. Run on `*.bin` it checks byte-exact rebuilds. A road-shape record shares the kind word but not the layout, and is refused |
| `clarion_rd.py` | read a section-2 road record: node lists with per-node link entries |
| `clarion_profile.py` | read a build profile; run it on a profile name to dump what the tools will see |
| `inflate_tile.py` | every zlib record of a tile as a file, one per record |
| `tile_scan.py` | walk a tile through mmap, one record at a time, for counting and searching a factory tile too large to load |
| `tile_grid.py` | check the name-to-grid formula over a directory of tiles |
| `bg_decode.py` | a tile's background geometry back to longitude and latitude, as GeoJSON |
| `bg_encode.py` | land polygons and coastline ways into background records and a tile |
| `bg_tiles.py` | background tiles for a profile's cells |
| `fa_encode.py` | the road-shape record that the renderer draws |
| `rd_encode.py` | a section-2 road record and directory from a node/link graph |
| `rd_build.py` | levels 1 and 2 of a whole cell from OSM ways: graphs, shapes and names |

A tile template only lends its 0x1000 header; every table is regenerated. Sea polygons with
holes (islets inside a parcel's sea) lose the holes. Either emit the islets as their own
land parcels at the finer level, or accept the gap.

## Still unknown

- Section 2: which link a road name belongs to (the second names sub-table), the meaning of
  w4/w5 and the extra words, and node flag 0x2000.
- Section 4 (type 0x93) in large tiles.
- Header words w3/w4 (`347a 2aef`) and the layer-4 group format `0002 0005`.
- Whether the unit needs section 2, `LEV3456R.DAT` entries, or the address database to show
  a tile at all.
- The order the original writer lays out directories and blocks. The reader records the
  original order so that real tiles rebuild byte-identically; new tiles get table,
  directories, blocks per level. The unit follows addresses, so this should not matter.
- House numbers and address search. Nothing in this chain writes the address database.
