#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy a card image cheaply where the filesystem can share blocks, and hash one."""
import hashlib
import os
import shutil
import subprocess
import sys


def clone(src, dst):
    """A 16 GB card in a moment on APFS (cp -c) or btrfs/XFS (--reflink); a plain copy
    elsewhere. The copy is made writable, since bases are kept read-only."""
    flag = '-c' if sys.platform == 'darwin' else '--reflink=always'
    if subprocess.run(['cp', flag, src, dst], stderr=subprocess.DEVNULL).returncode:
        shutil.copyfile(src, dst)
    os.chmod(dst, 0o644)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 24), b''):
            h.update(block)
    return h.hexdigest()


if __name__ == '__main__':
    for p in sys.argv[1:]:
        print('%s  %s' % (sha256(p), p))
