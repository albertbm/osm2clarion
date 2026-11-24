# A QY7 unit and its card: what is checked at start, and a card from another market

Measured on one unit, a 2011 North American Leaf (QY7200 class), with the 2011 European card
`QSD-309-101` (image SHA-256 `105e2032…76307`) and the 2011 North American card `QSD-301-103`
(`53342430…c5383`). The strings in the two tables were read off both card images again on
24 Nov 2026; the unit's own values come from its kernel image NK1. The log lines are the unit's own
serial output, read in an emulator booted from the card and a dump of that unit's flash; the
last section was then driven in the car (24 Nov 2026). One unit only: a later QY7 (the 2014
card `QSD-357-101` and its units) was not tried. The tools named here live in
[OSM2Leaf](https://github.com/neutrinus/OSM2Leaf), not in this repository, and nothing here
needs them to build a map.

## The card carries the software

A 2011 QY7 card is not one filesystem. Ahead of the map partition it holds three Windows CE
ROM images: one with the applications, `Navi.exe` among them, and two kernels with the updater
and the test mode. The unit's flash is 8 MiB and holds a boot loader, its own kernel (NK1), a
product record and the user data. NK1 loads the application image from the card at every
start.

Two things follow for a card build:

- The first 100 MB of the card, up to the map partition, must stay as on the base card.
- A change to how the navigation behaves (a consent screen, a position limit) is a change to
  the card's application image, not to the unit. That image has its own checksum, which a
  patch must recompute.

## Two checks before the map is opened

**Build name.** NK1 compares its own build name with the one in the header of the card's
application image, from the third character on.

| side | where | 2011 US unit | 2011 EU card | 2011 US card |
|---|---|---|---|---|
| unit | NK1 header + 0x20 | `X110NBNI.147` | | |
| card | header of the application image + 0x16, in both copies (card offsets 0x200016, 0x2000016) | | `X210ECNI.158` | `X210NBNI.147` |

A mismatch logs `NK Version Error. Try Next Store Region.` for both copies and then
`AppLch >> Error Proc<00000042>!!`. The name is outside the image checksum: the checksum
passes with the name changed.

**Map code.** The launcher then compares a code in NK1 with line 1 of
`VER_INFO/HDD_INFO.TXT` on the map partition.

| side | where | 2011 US unit | 2011 EU card |
|---|---|---|---|
| unit | NK1 header + 0x60 | `QSD-301-100` | |
| card | `VER_INFO/HDD_INFO.TXT`, line 1 | | `QSD-302-100` |

A mismatch logs `AppLch >> Error Proc<00000050>!!` and the screen shows `DIFFERENT MAP CODE`.
The card also keeps a 72-byte raw copy of that file at card offset 0x06000000, outside the
filesystem; the launcher's comparison reads the file.

Lines 1 to 3 of `HDD_INFO.TXT` on the two cards, as measured:

| line | EU card | US card |
|---|---|---|
| 1, the map code the launcher compares | `QSD-302-100` | `QSD-301-100` |
| 2, the card's own label | `QSD-309-101` | `QSD-301-103` |
| 3 | `02-110100-00` | `02-100100-00` |

Not part of either check on this unit: the DATA VERSION stamp of the map files. The European
card's `MMAIN001.DAT` carries `11/06/27/01`, the North American card's `10/08/12/01`, and the
unit drew the European map with its stamps unchanged. No string in the firmware's navigation
program matches a DATA VERSION, and `MMAIN001.DAT` is read for its length only. README.md says
the unit checks the stamp against its own memory; on this 2011 unit that was not seen, and a
QY8 unit may well behave differently.

## A European card on a North American unit

With both strings on the card set to the unit's values, the 2011 US unit starts the 2011 EU
card. Three more steps were needed before it was usable:

1. The unit keeps navigation state written under the old card in its flash (the first
   0x60000 bytes of the 1 MiB user-data block). With that state in place the European card
   crashed `Navi.exe` before the first map draw, 2 runs of 2. With that range erased, the
   firmware loaded defaults and drew the map. The rest of the user data was left as it was.
2. The European `Audio.exe` opens a tuner device that a North American unit never starts.
   Until the North American `Audio.exe` was put into the card's application image, the
   equalizer did not react.
3. The consent screen is the card's, so the patch that skips it is a card patch.

In the car: the European map draws, the clock is right, the equalizer and key beeps work.
Standing still, the car stays at the card's default position in London; after a few
kilometres above 10 km/h it moved to its real position in Wrocław. The navigation takes a
far GPS fix only from consistent moving samples, and in the emulator only with wheel pulses
that agree with them. FM radio works only in part, and the service menu does not open. The
unit's area setting stays North American: the map scale is in miles.

The step-by-step procedure, with the tools and the undo path, is
[docs/hardware/eu-card-on-us-unit.md](https://github.com/neutrinus/OSM2Leaf/blob/main/docs/hardware/eu-card-on-us-unit.md)
in OSM2Leaf. It writes the unit's flash, so it starts with a backup.

## Reading and writing the card

A 2011 QY7 card is locked with an SD card password (CMD42). A locked card answers no read,
so `dd` as in README.md fails until the card is unlocked, and a card written from an image
must be locked again with the same password or the unit does not read it. The password
belongs to the unit, not to the card: the unit's flash holds it, and
[LeafSDTools](https://github.com/developerfromjokela/leafsdtools), run on the unit, shows
it.

## Not established

- Whether the same two checks, at the same offsets, hold on a 2014 or 2016 QY7 unit.
- Whether any QY7 unit compares the DATA VERSION stamp. None was seen on this one.
- Which of the six objects in the erased range stopped the European card. Erasing all six
  works; one of them alone was shown to crash `Navi.exe`, and a second stops the first map
  draw.
- Why FM radio only partly works with the transplanted `Audio.exe`.
