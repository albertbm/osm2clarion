# Contributing

## Tests

```
python3 -m unittest discover -s tests
```

`tests/test_roundtrip.py` builds its own inputs and needs nothing but the standard library,
so it runs on a fresh clone. Run it before and after a change.

`tests/test_reproduce.py` checks the byte-for-byte claims against a base card, and skips
unless `OSM2CLARION_CARD` points at one. The project cannot distribute a card: the database
on it is licensed commercial data. If you have one, mount it read-only and see
[tests/README.md](tests/README.md).

## What must not be committed

No map data and no firmware, in any form. No tiles, no `.DAT` files off a card, no card
images, no extracted records, no NAND dumps, no decompressed address files. Not a fragment
as a test input either. `.gitignore` catches the usual extensions; it is not a substitute for
checking what you are about to add. Measurements, offsets, record layouts and digests are
fine, and are what `docs/` is made of.

## Format findings

A finding comes with the evidence that established it: which card, which file, how many
records it holds over, and what was compared against what. `docs/` is written that way
throughout, failures included, because the failures pinned down more of the container's rules
than the successes did. A claim about what the unit does needs the build that showed it and
what appeared on screen. If a tool checks itself against real data, say so and say how.

## Licence

Contributions are GPL-3.0-or-later, the licence of the tools. See
[LICENSES.md](LICENSES.md) for why a map you build from OpenStreetMap carries different
terms.
