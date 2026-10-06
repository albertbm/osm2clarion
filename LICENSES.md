# Three things, three sets of terms

They are not the same and must not be treated as the same.

## The tools in this repository

GPL-3.0-or-later. Copyright (C) 2026 Albert. The full text is in [LICENSE](LICENSE).

This covers `build.py`, everything in `tools/`, the profiles, the docs and the tests. Nothing
else.

## A map you build from OpenStreetMap

ODbL 1.0, because the data going in is OpenStreetMap's. The tools' licence has no bearing on
it. If you distribute a card or an image you built, the ODbL's attribution and share-alike
terms apply to you: credit OpenStreetMap contributors, and offer the derived database under
the same terms. See <https://www.openstreetmap.org/copyright>.

## The navigation database on a factory card

Licensed commercial data, NAVTEQ 14Q2 or 17Q2 packaged by Zenrin for Clarion. No licence in
this repository grants anything over it. It is not redistributable, and nothing of it is
committed here: no tiles, no headers, no extracted bytes. `docs/FORMAT.md` records what was
measured off such a card, not the card's contents.

A built card normally holds both: your ODbL map in the tiles you wrote, and the shipped
commercial database in every file you did not touch. Distributing that card distributes both,
so it cannot be distributed.
