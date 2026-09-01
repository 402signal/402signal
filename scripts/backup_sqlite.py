#!/usr/bin/env python3
"""Online SQLite snapshot of catalog, history, and pq-log.

Single-writer only. Uses the SQLite backup API. Does not rewrite
the live files. Does not claim Fly scheduled snapshots are active.

  LIVE402_FIXTURE=1 PYTHONPATH=. python3 scripts/backup_sqlite.py --dest /tmp/402signal-backup
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


def _src(env_name: str, default: str) -> Path:
    raw = (os.environ.get(env_name) or "").strip()
    return Path(raw or default)


def _backup_one(src: Path, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    if not src.is_file():
        return {"ok": False, "reason": "missing"}
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
        dst_conn = sqlite3.connect(str(dest), timeout=5.0)
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
    except sqlite3.Error:
        return {"ok": False, "reason": "backup_failed"}
    return {"ok": True, "bytes": dest.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup 402Signal sqlite files")
    parser.add_argument("--dest", required=True, help="Directory for snapshot files")
    args = parser.parse_args(argv)
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    jobs = (
        ("catalog", _src("LIVE402_CATALOG_DB", "/tmp/catalog.sqlite"), dest_dir / f"catalog-{stamp}.sqlite"),
        ("history", _src("LIVE402_HISTORY_DB", "/tmp/live402-history.sqlite"), dest_dir / f"history-{stamp}.sqlite"),
        ("pq_log", _src("LIVE402_PQ_LOG_DB", "/tmp/pq-log.sqlite"), dest_dir / f"pq-log-{stamp}.sqlite"),
    )
    ok = True
    for name, src, dest in jobs:
        result = _backup_one(src, dest)
        status = "ok" if result.get("ok") else result.get("reason")
        print("%s %s" % (name, status))
        if not result.get("ok"):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
