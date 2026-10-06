#!/usr/bin/env python3
"""Unpack the .cmp/.ind pairs of a card's address half.

Nothing in the build chain needs this: `MAPAL001` holds no .cmp or .ind at all, so the map
surface is never compressed. It is here to read what the rest of the card carries -- the
address and postcode database the unit searches -- when a question about a base card comes up.

The container is dfj's, from the gist "Clarion Maps structure + decompressor of CMP files":
a block table in the .ind and zlib blocks in the .cmp. His script slices comp_data[block:]
for every block, copying the whole remaining buffer each time, which on a 300 MB file of
about 4,600 blocks is hundreds of gigabytes of memcpy and never finishes. A block ends where
the next one starts, so slicing to that offset makes it linear. This also writes one output
file per input rather than a directory holding data.bin, and keeps one file handle open
instead of reopening it per block.

    ./decompress_fast.py <map dir> <out dir> [name filter]

The filter is a substring — `./decompress_fast.py <card> <out dir> ZCODE`
unpacks only the postcode files.
"""

import json
import os
import sys
import zlib
from pathlib import Path


def read_index(path):
    data = Path(path).read_bytes()
    offsets = [int.from_bytes(data[i:i + 4], "little")
               for i in range(0x10, len(data), 4)]
    return {
        "uncompressed_size": int.from_bytes(data[0:4], "little"),
        "compressed_size": int.from_bytes(data[4:8], "little"),
        "blocks_count": data[8],
        "meta_data": data[9:0x10].hex(),
        "offsets": offsets,
    }


def compressed_list(base):
    """CMP.LST is NUL-separated Windows paths under \\SystemSD\\."""
    lst = Path(base) / "CMP.LST"
    if not lst.exists():
        return ["REFER001/" + n[:-4] for n in
                sorted(os.listdir(Path(base) / "REFER001")) if n.endswith(".ind")]
    names = [p.replace("\\SystemSD\\", "").replace("\\", "/")
             for p in lst.read_bytes().split(b"\x00")[:-1]
             for p in [p.decode("ascii")] if p]
    return names


def unpack(base, name, out_dir):
    ind = read_index(os.path.join(base, name + ".ind"))
    offsets = ind["offsets"]
    src = os.path.join(base, name + ".cmp")
    dst = Path(out_dir) / name
    dst.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(src, "rb") as f, open(dst, "wb") as out:
        blob = memoryview(f.read())
        for i, start in enumerate(offsets):
            end = offsets[i + 1] if i + 1 < len(offsets) else len(blob)
            block = zlib.decompress(blob[start:end])
            out.write(block)
            written += len(block)

    ok = written == ind["uncompressed_size"]
    meta = {k: v for k, v in ind.items() if k != "offsets"}
    meta["written"] = written
    meta["size_match"] = ok
    Path(str(dst) + ".info.json").write_text(json.dumps(meta, indent=2))
    return written, ok


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    base, out_dir = sys.argv[1], sys.argv[2]
    needle = sys.argv[3] if len(sys.argv) > 3 else ""

    names = [n for n in compressed_list(base) if needle in n]
    total = bad = 0
    for i, name in enumerate(names, 1):
        written, ok = unpack(base, name, out_dir)
        total += written
        if not ok:
            bad += 1
        print(f"[{i:3}/{len(names)}] {name:32} {written:>10,} bytes"
              f"{'' if ok else '  SIZE MISMATCH'}", flush=True)

    print(f"\n{len(names)} files, {total / 2**30:.2f} GiB, {bad} size mismatches")


if __name__ == "__main__":
    main()
