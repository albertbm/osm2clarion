#!/usr/bin/env python3
"""Generate DRSTR001.DAT, the directory index of a Clarion map database.

The head unit reads this rather than scanning the card, so a database with new
tiles needs a matching one or the tiles are invisible.

Format, worked out 22 Sep 2026 against a 2017-vintage European card (volume
GEURX-30-105-100, database 17-33, index 466,952 bytes) and verified by regenerating
that file byte for byte:

    directory records, 0x28 bytes each, from offset 0, one per directory
        0x00  char[20]   name, NUL-padded
        0x14  uint16 LE  own id, 1-based, in record order
        0x16  uint16 LE  PARENT id. Tree linkage, not a type. The root's
                         parent is itself.
        0x18  uint16 LE  child count
        0x1a  10 bytes   zero
        0x24  uint32 LE  absolute offset of this directory's child block

    child blocks follow the record array, in record order, 0x20 bytes each
        0x00  char[20]   name
        0x14  uint16 LE  zero
        0x16  uint16 LE  2 = directory, 0 = file
        0x18  uint32 LE  zero
        0x1c  uint32 LE  size in bytes. For a directory this is the sum of its
                         children, TRUNCATED TO 32 BITS. MAPAL001 really holds
                         5,162,906,624 bytes and the field reads 867,939,328.

Ordering is the on-disk FAT directory order, not alphabetical. OBJCT001 lists
OBJCTIDX.DAT before its subdirectories, exactly as readdir returns it. The one
exception is VER_INFO, which is hoisted to the front of its parent.

    ./make_drstr.py <map root> [output file]
"""

import os
import struct
import sys
from pathlib import Path

DIR_REC = 0x28
CHILD_REC = 0x20
NAME_LEN = 0x14

# The card's own index omits these, and must: SPEECH holds 33-character
# filenames that will not fit the 20-byte name field, and a file cannot list
# itself because writing its size would change its size.
SKIP = {"SPEECH", "DRSTR001.DAT"}


def listing(path):
    """What the index shows for a directory.

    Compressed files are listed LOGICALLY: the card has ADDRE001.DAT.cmp and
    ADDRE001.DAT.ind on disk, but the index carries one ADDRE001.DAT entry whose
    size is the UNCOMPRESSED size, read from the .ind header. So the index
    describes the database as the unit sees it, with compression resolved.
    """
    out = []
    for e in os.scandir(path):
        if e.name in SKIP or e.name.endswith(".ind"):
            continue
        if e.name.endswith(".cmp"):
            ind = Path(str(e.path)[:-4] + ".ind")
            size = struct.unpack("<I", ind.read_bytes()[:4])[0]
            out.append((e.name[:-4], False, size))
        elif e.is_dir():
            out.append((e.name, True, tree_size(Path(e.path))))
        else:
            out.append((e.name, False, e.stat().st_size))
    # FAT order as mastered, except VER_INFO, which the card hoists to the front;
    # the unit presumably wants the version before anything else.
    out.sort(key=lambda r: r[0] != "VER_INFO")
    return out


def name_field(name):
    raw = name.encode("ascii")
    if len(raw) > NAME_LEN:
        raise ValueError(f"name too long for the 20-byte field: {name}")
    return raw.ljust(NAME_LEN, b"\x00")


def scan(root):
    """Walk the tree breadth-first, matching the record order on the card."""
    dirs = []           # (path, name, parent_id)
    queue = [(Path(root), "/", 1)]
    while queue:
        path, name, parent = queue.pop(0)
        my_id = len(dirs) + 1
        dirs.append((path, name, parent))
        for child_name, is_dir, _ in listing(path):
            if is_dir:
                queue.append((path / child_name, child_name, my_id))
    return dirs


def tree_size(path):
    """Sum of what the index LISTS, not of what is on disk, so a compressed
    file contributes its uncompressed size. REFER001 reads 1,251,633,152 this
    way against 3,953,262,803 of actual .cmp/.ind bytes."""
    return sum(size for _, _, size in listing(path))


def build(root):
    dirs = scan(root)
    blocks = []
    for path, _, _ in dirs:
        blocks.append([(n, d, s & 0xFFFFFFFF) for n, d, s in listing(path)])

    out = bytearray(len(dirs) * DIR_REC)
    offset = len(out)
    for i, ((_, name, parent), rows) in enumerate(zip(dirs, blocks)):
        struct.pack_into("<20sHHH", out, i * DIR_REC,
                         name_field(name), i + 1, parent, len(rows))
        struct.pack_into("<I", out, i * DIR_REC + 0x24, offset)
        offset += len(rows) * CHILD_REC

    for rows in blocks:
        for name, is_dir, size in rows:
            out += struct.pack("<20sHHII", name_field(name), 0,
                               2 if is_dir else 0, 0, size)
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("DRSTR001.DAT")
    data = build(root)
    dest.write_bytes(data)
    print(f"wrote {dest}, {len(data)} bytes")


if __name__ == "__main__":
    main()
