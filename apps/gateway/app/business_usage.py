#!/usr/bin/env python3
"""Publish immutable closed-day usage revisions into the local outbox.

Run from the business worker's private scheduled job. No network call occurs.
Restarting/replaying a page reuses unchanged daily snapshots.
"""
import argparse
from datetime import date


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    from .platform import app  # register all existing models
    from .main import DB
    from .business_events import snapshot_page
    cursor = ""
    while True:
        with DB() as session:
            cursor = snapshot_page(session, args.day, cursor)
            session.commit()
        if cursor is None:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
