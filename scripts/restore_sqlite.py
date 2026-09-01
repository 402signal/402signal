#!/usr/bin/env python3
"""Restore catalog, history, or pq-log from a backup_sqlite.py snapshot.

Stop the app first. One writer. This replaces the destination file; it does
not rewrite log leaves or mutate v1 events. Confirm the snapshot hash before
running.

  PYTHONPATH=. python3 scripts/restore_sqlite.py --src snapshot.sqlite --dest /data/catalog.sqlite
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore one 402Signal sqlite file")
    parser.add_argument("--src", required=True, help="Snapshot produced by backup_sqlite.py")
    parser.add_argument("--dest", required=True, help="Live sqlite path to replace")
    parser.add_argument("--force", action="store_true", help="Overwrite dest after integrity check")
    args = parser.parse_args(argv)
    src = Path(args.src)
    dest = Path(args.dest)
    if not src.is_file():
        print("src missing")
        return 1
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
        row = conn.execute("PRAGMA integrity_check(1)").fetchone()
        conn.close()
    except sqlite3.Error:
        print("src not a readable sqlite file")
        return 1
    if not row or str(row[0]).lower() != "ok":
        print("src failed integrity_check")
        return 1
    if dest.exists() and not args.force:
        print("dest exists; pass --force after stopping the single writer")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    for extra in (str(dest) + "-wal", str(dest) + "-shm"):
        try:
            os.remove(extra)
        except OSError:
            pass
    print("restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
