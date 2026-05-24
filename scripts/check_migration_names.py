#!/usr/bin/env python3
"""Validate that all db/migrations/*.sql files use a 4-digit zero-padded prefix."""

import pathlib
import re
import sys

bad = [
    p.name
    for p in sorted(pathlib.Path("db/migrations").glob("*.sql"))
    if not re.match(r"^\d{4}_", p.name)
]
if bad:
    print("ERROR: Migrations with non-4-digit prefix:", bad)
    sys.exit(1)

print("OK: all migration files comply with 4-digit prefix rule.")
