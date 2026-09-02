#!/usr/bin/env python3
"""Print leaf/checkpoint/authorized/confirmed counts only.

Never dumps leaf bodies, signed blobs, or secrets. Never copies an old
tree into a new file. Optional --init-empty creates a fresh schema at
--dest when the file is missing or already empty.

  LIVE402_FIXTURE=1 PYTHONPATH=. python3 scripts/pq_log_fresh_state.py \\
      --db /tmp/pq-log-mainnet.sqlite
  LIVE402_PQ_LOG_EPOCH=mainnet-v1 PYTHONPATH=. python3 scripts/pq_log_fresh_state.py \\
      --init-empty --dest /tmp/pq-log-mainnet.sqlite
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def inspect_counts(path: Path) -> dict:
    if not path.is_file():
        return {
            "exists": False,
            "leaves": 0,
            "checkpoints": 0,
            "authorized": 0,
            "confirmed": 0,
            "origin": "",
        }
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5.0)
    try:
        origin = ""
        try:
            row = conn.execute("SELECT v FROM meta WHERE k = 'origin'").fetchone()
            origin = str(row[0]) if row and row[0] else ""
        except sqlite3.Error:
            origin = ""
        return {
            "exists": True,
            "leaves": _count(conn, "leaves"),
            "checkpoints": _count(conn, "checkpoints"),
            "authorized": _count(conn, "authorized_anchors"),
            "confirmed": _count(conn, "confirmed_anchors"),
            "origin": origin,
        }
    finally:
        conn.close()


def is_fresh(counts: dict) -> bool:
    return (
        int(counts.get("leaves") or 0) == 0
        and int(counts.get("checkpoints") or 0) == 0
        and int(counts.get("authorized") or 0) == 0
        and int(counts.get("confirmed") or 0) == 0
    )


def init_empty(dest: Path) -> dict:
    """Create empty MainNet-shaped schema. Refuse to overwrite a non-empty file."""
    if dest.is_file():
        counts = inspect_counts(dest)
        if not is_fresh(counts):
            raise SystemExit("refusing to overwrite non-empty log")
        return counts
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LIVE402_PQ_LOG_EPOCH", "mainnet-v1")
    os.environ["LIVE402_PQ_LOG_DB"] = str(dest)
    from live402.pq import store

    store.close()
    store.size()
    store.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return inspect_counts(dest)


def _print_counts(counts: dict) -> None:
    print("exists=%s" % ("yes" if counts.get("exists") else "no"))
    print("leaves=%s" % counts.get("leaves", 0))
    print("checkpoints=%s" % counts.get("checkpoints", 0))
    print("authorized=%s" % counts.get("authorized", 0))
    print("confirmed=%s" % counts.get("confirmed", 0))
    if counts.get("origin"):
        print("origin=%s" % counts["origin"])
    print("fresh=%s" % ("yes" if is_fresh(counts) else "no"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PQ log fresh-state counts (no dumps)")
    parser.add_argument("--db", help="Existing sqlite to inspect (counts only)")
    parser.add_argument("--init-empty", action="store_true", help="Create empty schema at --dest")
    parser.add_argument("--dest", help="Destination for --init-empty")
    args = parser.parse_args(argv)
    if args.init_empty:
        dest = Path(args.dest or args.db or "")
        if not dest:
            print("dest required")
            return 2
        counts = init_empty(dest)
        _print_counts(counts)
        return 0 if is_fresh(counts) else 1
    if not args.db:
        print("db required")
        return 2
    counts = inspect_counts(Path(args.db))
    _print_counts(counts)
    return 0 if is_fresh(counts) else 1


if __name__ == "__main__":
    sys.exit(main())
