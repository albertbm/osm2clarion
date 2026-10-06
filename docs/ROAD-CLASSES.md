# Road classes and widths

The renderer takes a road's drawn width, colour and casing from one number: the class, in the
top nibble of shape word 2. The layer of the geometry unit and the style word follow from it
too. Get it wrong and every road draws as a 1 px line. Measured on factory tiles of the 2014
and 2017 databases, which differ in six header-string bytes, so one holds for both.

## Where the class sits

A level-1 road shape, inside record kind 0x00fa, the even pair of a level-1 background
directory:

| word | contents |
|---|---|
| 0 | length in words, itself included |
| 1 | `0x8000 \| point count` |
| 2 | `class << 12`, low 12 bits zero |
| 3, 5, 6 | `0x0004`, `0x8004`, `0x0000` at level 1; all three zero at level 2 |
| 4 | name entry offset in words, `0xffff` for none |
| 7, 8 | chain start point, `x` and `0x2800 \| y` |
| 9, 10, 11 | style word `0x101e`, `0x0100`, link id |
| 12, 13 | first point of the first link |

Words 9 to 11 repeat per link of the chain. Coordinates live in words 7, 8, 12, 13 and up,
nowhere near word 2, and that independence is measured: a rebuild changing only class, layer
and style compared all 209,295 level-1 shapes of four tiles point by point, maximum delta 0.

## Which layer a class belongs to

Shapes are grouped by layer in the geometry unit and each layer gets one (offset, count) pair,
the offset in dwords from the unit start. Level 1 has a count word, twelve pairs and a pad
word, so shapes start at dword 13; levels 2, 3 and 4 have eleven pairs and no count word, so
dword 11. A group of an odd word count is followed by a pad word: over one 2014 tile's
multi-layer level-1 records, 1,158 odd groups are and 993 even ones are not.

Layer and class pairs read off that tile, 9,000 level-1 records and all 2,127 level-2 records:

```
level 1   layer 0 class c     764    ferries
          layer 4 class 9    2835
          layer 5 class 8  129388
          layer 6 class 3       2
          layer 6 class 4      77    all 77 carry a route number
          layer 6 class 5    1982    all 1982 carry a route number
level 2   layer 0 class a       1
          layer 6 class 3     269
          layer 6 class 4    4290
          layer 6 class 5    6511
          layer 6 class 7    7541    the largest group; names, mostly no route number
          layer 7 class 1      16
          layer 7 class 2       4
```

Layer 7 takes classes 1-2, layer 6 takes 3-7, layer 5 class 8, layer 4 class 9, layer 0 ferries.

## The highway mapping

`tools/fa_encode.py` `CLASSES`, with the level-3 and level-4 classes from `tools/lev_roads.py`
(`CLASS_CODE3` and `CLASS_CODE`):

| OSM `highway` | L1 layer | L1 class | L2 class | L3 class | L4 class |
|---|---|---|---|---|---|
| `motorway`, `motorway_link` | 7 | 1 | 1 | 3 | 3 |
| `trunk`, `trunk_link` | 6 | 4 | 3 | 4 | 4 |
| `primary`, `primary_link` | 6 | 5 | 4 | 5 | 5 |
| `secondary`, `secondary_link` | 6 | 5 | 5 | 6 | 6 |
| `tertiary`, `tertiary_link` | 5 | 8 | 7 | 7 | n/a |
| `unclassified`, `residential`, `living_street` | 5 | 8 | 7 | 7 | n/a |
| `service`, `track` | 4 | 9 | 7, never used | 7 | n/a |
| anything else | 5 | 8 | 7 | 7 | n/a |

The level-2 layer is 7 for classes 1 and 2, otherwise 6; levels 3 and 4 are always layer 6.
Only motorway, trunk and primary reach level 4 by default, and `tertiary` is the lowest value
that reaches level 3 at all: `lev_roads.py` drops unclassified and below whatever level-3 class
the table gives them. `service` is excluded from level 2 by `rd_build.L2CLASSES`, so its 7
there never fires. Class follows the road's standard, not its route prefix, since in the 2014
data every layer-6 class-4 and class-5 level-1 shape carries a route number and nothing
numbered sits below class 5. `fa_encode.CLASSES` also holds a level-3 column that
`lev_roads.py` never reads; keep the two in step.

## Which classes reach level 2

`rd_build.py --l2-classes` picks the set. Narrow is motorway, trunk and primary with their
links; wide, the default, adds secondary and tertiary. Record counts cannot decide between
them. Shipped level-2 to level-1 road-record ratios run from 5.9% (B60R060R, 1368 and 81) to
26.1% (B58R070R, 23 and 6), with 7.8% on the big Norway tile (27362 and 2127); over the four
Iceland cells wide gives 10.7-15.9% and narrow 6.4-8.4%, both inside that range.

The class mix decides instead. Class 7, the ordinary-street class, is the largest level-2
group in shipped data, 7,541 of that tile's 18,632 level-2 shapes. No road in the narrow set
maps to class 7, so narrow gives a level 2 with no streets in it, and streets are most of what
the 500 m to 1 km view of a town is made of. Hence wide. One four-tile build of Iceland:

```
level 1   before   209295 shapes   layer 5 class 8  209295              style 0x1031
          after    209295 shapes   layer 5 class 8  130252              style 0x101e
                                   layer 6 class 4   11940  trunk
                                   layer 6 class 5   67103  primary, secondary
level 2   before    35180 shapes   layer 6 class 7   35180              style 0x101e
          after    115480 shapes   layer 6 class 3   11095  trunk
                                   layer 6 class 4   24085  primary
                                   layer 6 class 5   39188  secondary
                                   layer 6 class 7   41112  tertiary
```

The before column is the defect in one line: every level-2 shape was class 7, the minor-street
class, and they were all trunk and primary roads.

## service and track

`service` belongs at level 1 and nowhere else. Layer and class tally over 52 factory tiles of
the 2014 card, 713,293 level-1 and 76,992 level-2 road-shape records, 12.4 M level-1 and
2.36 M level-2 shapes:

```
level 1   layer 4 class 9   1,971,729 shapes   18,377,952 pts   16.8% of all level-1 shapes
          layer 5 class 8   9,792,514 shapes   80,913,727 pts
          layer 6 class 7     219,760 shapes
level 2   layer 4            0 shapes in any class
          class 9            1 shape, in layer 6, out of 2,357,489
```

Class 9 is level-1 only in shipped data and layer 4 is empty at level 2 in all 52 tiles, hence
the `L2CLASSES` exclusion. At level 1 `service` costs about 8% of tile size. Over
Iceland, whose four tiles cover the whole country, `service` is 34,104 of 62,439 ways, 4,979.9
of 22,734.4 km and 241,699 of 690,696 raw points. The four tiles grow by 128,768, 459,776,
1,025,792 and 1,899,008 B, 43,896,320 to 47,409,664 B in all: +3,513,344 B, +8.0%, or 3,538,944
B rounded up to the 64 KiB FAT32 cluster. Level-1 parcels with a road record go from 7,929 to
8,448 and level-1 shapes from 209,295 to 327,368; level 2 does not change.

`track` stays out, and `tools/osm_roads.py` `KEEP` keeps it out of the extract, so the `track`
row in `CLASSES` has never fired. Counted from an Iceland PBF: 13,534 track ways, 14,008.8 km,
402,601 raw points, 1.66x the geometry of `service` and +93% points on a database holding
690,696. Only 121 of them are tracktype grade1; the rest are gravel, ground, grass, dirt or
untagged, since `track` in Iceland is the F-road and farm-track tag, and class 9 would draw an
unpaved mountain track in the same thin line as a car-park aisle. Adding them later is two
edits: `track` into `KEEP`, then re-run `osm_roads.py`.

## Level-4 shapes go in layer 6

No factory level-4 record puts road shapes in layer 5. Over the 359 level-4 road-block records
of a 2014 factory file, layer 0 holds 2,385 shapes, layer 6 holds 236,517 and layer 7 holds
31,864, while layer 5 is empty. The layer-6 classes are 3 (320), 4 (7,742), 5 (5,855) and 6
(13,456); layer 7 is class 1 (145) and class 2 (250).

An early build wrote the pair at table index 10/11, which is layer 5, and put 4,434 level-4
shapes there, 1,831 class 4 and 2,603 class 5, over 17 records. `tools/lev4_layer.py` moves
the pair in a built `LEV3456R.DAT` without a rebuild. Four words of the layer table change,
six bytes differ, and the record's plain length holds:

```
before  ffff 0000 ... ffff 0000 000b 0001 ffff 0000 ...
after   ffff 0000 ... ffff 0000 ffff 0000 000b 0001 ...
```

Compared record by record over the whole file, 18,427 to 18,758 records depending on the
database, the key sets match and exactly 17 records differ, all section-1 level 4; layer 6 rises
by 4,434 and layer 5 drops to 0.

## Style word, names and the graph class word

`0x101e` is the common level-1 style: over the 2014 sample, `0x101e` 79,182, `0x105e` 38,752,
`0x102d` 9,655, `0x005e` 464. At level 2, `0x101e` 4,052 and `0x001e` 3,397. `0x1031`, which an
early build wrote everywhere, appears twice in 378,762 shipped shapes.

Service roads carry names in shipped data, so naming them is not clutter we invented. Of
factory layer-4 class-9 shapes, 3,877 of 7,371 are named in a Norway tile (52.6%) and 493 of
1,089 in a Greek one (45.3%), with style words mixed across `0x101e`, `0x105e`, `0x005e` and
`0x001e`. Label text is one string, parts joined by `;`: `1=LYSEFJORDBRAU;2=13`, or `2=912`
alone when a road has a number but no name.

Section-2 node entries carry a class word of their own, `cls << 8 | 0x90 | cls`, and the
shipped distribution follows that rule: 0x898 102,045, 0x595 23,070, 0x999 16,512, 0x494 9,194,
0x393 3,254, 0x797 768. It is routing data the renderer never reads, so it changes no width.

## Node degree stops at 15

`rd_encode` packs a node's entry count into the top nibble of the word whose low bits hold x,
so a 16th entry silently corrupts the x coordinate. Both record builders assert
`len(ents) <= 15`, which changes no output. Degree histograms over the four Iceland cells:

```
skip service   max 6    1:19,739  2:197,480  3:8,278   4:928   5:18  6:5
service in     max 15   1:64,021  2:279,769  3:18,591  4:1,750 5:38  6:7  12:1  15:2
```

Adding `service` takes the worst node from degree 6 to 15, exactly what the nibble holds and no
margin at all. The three worst nodes are service-only and sit in one car-park and farmyard
cluster. A refresh that adds one more way there now fails instead of misplacing a road.

## Two deliberate omissions

`rd_build.py --no-l2-graph` is what the builds measured here used. Level-2 shapes draw without
the section-2 level-2 directories, the graph is routing data, no router runs at level 2, and
adding four directories would change the section-2 layout. Shapes first, graph later.

`trunk` is level-1 class 4 and level-2 class 3, the heaviest numbered style. That suits a dual
carriageway; on a single-carriageway national route it can look overdone. The change is one
line in `fa_encode.CLASSES`: give `trunk` and `trunk_link` the same entry as `primary`,
`((6, 5), 4, 5)`, and they draw like everything else numbered.
