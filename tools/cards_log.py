#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The cards log: one table row per build number, newest first, twins on one row."""
import os
import time

HEADER = ('# Cards\n\n'
          '| no. | date | units | slug | what changed | status |\n'
          '|---|---|---|---|---|---|\n')


def add(path, n, unit, slug, what, base, cards):
    """Add the build to the top of the log, or add the unit to its twin's row. A missing log
    is created with the table header."""
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(HEADER)
    with open(path) as f:
        lines = f.read().split('\n')
    head = next((i for i, l in enumerate(lines) if l.startswith('|---')), None)
    if head is None:
        raise SystemExit('%s has no table (a |--- line)' % path)
    for i in range(head + 1, len(lines)):
        cells = [c.strip() for c in lines[i].split('|')]
        if len(cells) > 4 and cells[1] == '%03d' % n and cells[4] == slug:
            cells[3] = ', '.join(sorted(set(cells[3].split(', ')) | {unit}))
            lines[i] = '| ' + ' | '.join(cells[1:-1]) + ' |'
            break
    else:
        shown_base = os.path.relpath(base, cards) if os.path.commonpath((base, cards)) == cards else base
        what = 'Base `%s`; %s' % (shown_base, what)
        lines.insert(head + 1, '| %03d | %s | %s | %s | %s | untested |'
                     % (n, time.strftime('%Y-%m-%d'), unit, slug, what))
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(lines))
    os.replace(tmp, path)
