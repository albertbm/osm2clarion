<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# A generic address importer

Not built. This is the shape it should take.

`addre_io`, `zcode_io` and `icode_io` can write an address database, but nothing
here reads one in. The Iceland build does that itself, against the Icelandic
national registry, so the format knowledge and the country knowledge are tangled
in a script this repo does not hold.

Anyone with address data for their own country should be able to build a card.
Getting the data is their problem; ingesting it should not be.

## The input

One flat row per address, as CSV or JSON lines, the same fields either way:

```
country_iso,level1,level2,level3,level4,street,number,suffix,postcode,lat,lon
352,Höfuðborgarsvæðið,Reykjavík,,,Þorláksgeisli,17,,113,64.12445,-21.74557
```

Levels run outermost first and may be blank. **The ladder's depth is whatever the
data has**, not a fixed set of names: Andorra is 3 deep, the UK 4, Italy 5, and
`df[0x49]` has to match. Calling the columns `level1..n` rather than `region`,
`province` and `city` keeps the importer out of the business of deciding what a
country's divisions are called.

Coordinates are per address and required. A source that has none needs a join
against road geometry first, which is the caller's problem, not this repo's.

## What the importer does

All of it format work, none of it country-specific:

- build the tier tree and the ICODE chains from the distinct level values
- fold keys, stripping the diacritic one character for one, and keep the display
  names as they came in
- group house numbers into odd and even ranges per street per postcode
- emit `ADDRE0nn`, `ZCODE0nn`, the ICODE tier table, the `COORD001` row and the
  `.cmp`/`.ind` wrap

## What it cannot infer

The tier **labels**. The ISO entry carries seven slots of text -
`COUNTRY / PARROQUIA / SETTLEMENT` for Andorra,
`COUNTRY / REGION / PROVINCE / COMMUNIONE / BUILT-UP AREA` for Italy. The unit
shows them and no amount of address data implies them. So a small config beside
the rows: the ISO code, the tier labels in order, and which ordinal to write into.

## What stays outside

The adapter that produces the rows. For the Icelandic registry it is about thirty
lines - street, number, suffix and postcode are columns, and the town comes from
joining the postcode file. Someone starting from OSM writes a different adapter
and reaches the same rows. That is the whole point of the split.
