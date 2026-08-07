#!/usr/bin/env python3
"""Fail when bundled model-catalog entries have gone unreviewed for too long.

Runs from the scheduled maintenance workflow, never from pull-request CI: what
turns this check red is the passage of time, not a change in the tree, and a
time-triggered failure on an unrelated change teaches people to ignore the
check. A red run means: re-verify the listed entries against their providers
and update ``last_reviewed`` in
``libs/jarvis_common/jarvis_common/data/model_catalog.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_CATALOG = Path("libs/jarvis_common/jarvis_common/data/model_catalog.json")
DEFAULT_MAX_AGE_DAYS = 90


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="Reference date override (tests only; defaults to the current date).",
    )
    args = parser.parse_args(argv)

    try:
        entries = json.loads(args.catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read catalog {args.catalog}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(entries, list) or not entries:
        print(f"error: {args.catalog} holds no catalog entries", file=sys.stderr)
        return 2
    if not all(isinstance(entry, dict) for entry in entries):
        print(f"error: {args.catalog} entries must be objects", file=sys.stderr)
        return 2

    today = args.today or date.today()
    threshold = timedelta(days=args.max_age_days)
    stale: list[str] = []
    for entry in entries:
        identifier = entry.get("id", "<missing id>")
        try:
            reviewed = date.fromisoformat(str(entry.get("last_reviewed", "")))
        except ValueError:
            stale.append(f"{identifier}: invalid last_reviewed {entry.get('last_reviewed')!r}")
            continue
        age = today - reviewed
        if age > threshold:
            stale.append(f"{identifier}: last reviewed {reviewed} ({age.days} days ago)")

    if stale:
        print(f"{len(stale)} of {len(entries)} catalog entries need review:")
        for line in stale:
            print(f"  {line}")
        return 1
    print(f"all {len(entries)} catalog entries reviewed within {args.max_age_days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
