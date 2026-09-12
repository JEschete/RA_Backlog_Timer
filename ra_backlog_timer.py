#!/usr/bin/env python3
"""RA Backlog Timer - compatibility entry point.

The implementation now lives in the `ra_backlog` package (see pyproject.toml
for the `ra-backlog` console script). This shim keeps `python ra_backlog_timer.py`
working for anyone with the old invocation in muscle memory or a shortcut.
"""
import sys

from ra_backlog.cli.args import main

if __name__ == "__main__":
    sys.exit(main())
