# SPDX-License-Identifier: GPL-3.0-or-later
"""--help for the tools that read sys.argv themselves."""
import sys


def help_exit(doc):
    """Print the tool's docstring and exit 0 when -h or --help is among the arguments."""
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print((doc or '').strip())
        raise SystemExit(0)
