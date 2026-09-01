#!/usr/bin/env python3
"""Fixture backup/restore identity drill for the PQ log.

Compares tree size, Merkle root, and checkpoint notes after restore.
Refuses /data production paths. Does not set broadcast flags. Does not
dial MainNet. Intended for tests and workstation copies.

  LIVE402_FIXTURE=1 PYTHONPATH=. python3 scripts/pq_log_restore_drill.py \\
      --src /tmp/pq-log-drill-src.sqlite --dest-dir /tmp/pq-log-drill-out
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION = (
    Path("/data/pq-log.sqlite"),
    Path("/data/pq-log-mainnet.sqlite"),
)


def _load_script(name: str):
    path = _ROOT / "scripts" / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _refuse_production(path: Path, label: str) -> None:
    resolved = path.resolve()
    text = str(resolved)
    if text.startswith("/data/") or text == "/data":
        raise SystemExit("%s refuses /data production path" % label)
    for prod in _PRODUCTION:
        try:
            if resolved == prod.resolve():
                raise SystemExit("%s refuses production sqlite" % label)
        except OSError:
            continue


def _identity(db_path: Path) -> dict:
    from live402.pq import store

    os.environ["LIVE402_PQ_LOG_DB"] = str(db_path)
    store.reset()
    try:
        size = int(store.size() or 0)
        root = store.root(size).hex() if size >= 0 else ""
        origin = store.origin()
        latest = store.latest_checkpoint()
        notes = []
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT size, note FROM checkpoints ORDER BY size").fetchall()
        except sqlite3.Error:
            rows = []
        conn.close()
        for size_i, note in rows:
            notes.append({"size": int(size_i), "note": str(note or "")})
        return {
            "size": size,
            "root": root,
            "origin": origin,
            "checkpoint": latest,
            "checkpoints": notes,
        }
    finally:
        store.close()


def compare_identity(src: dict, restored: dict) -> list[str]:
    mismatches = []
    for key in ("size", "root", "origin", "checkpoint"):
        if src.get(key) != restored.get(key):
            mismatches.append(key)
    if src.get("checkpoints") != restored.get("checkpoints"):
        mismatches.append("checkpoints")
    return mismatches


def run_drill(src: Path, dest_dir: Path) -> dict:
    _refuse_production(src, "src")
    _refuse_production(dest_dir, "dest-dir")
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = _identity(src)
    backup = _load_script("backup_sqlite")
    restore = _load_script("restore_sqlite")
    os.environ["LIVE402_PQ_LOG_DB"] = str(src)
    os.environ.setdefault("LIVE402_HISTORY_DB", str(dest_dir / "missing-history.sqlite"))
    os.environ.setdefault("LIVE402_CATALOG_DB", str(dest_dir / "missing-catalog.sqlite"))
    rc = backup.main(["--dest", str(dest_dir)])
    snaps = sorted(dest_dir.glob("pq-log-*.sqlite"))
    if not snaps:
        raise SystemExit("backup produced no pq-log snapshot (rc=%s)" % rc)
    restored = dest_dir / "pq-log-restored.sqlite"
    _refuse_production(restored, "restore dest")
    if restore.main(["--src", str(snaps[-1]), "--dest", str(restored), "--force"]) != 0:
        raise SystemExit("restore failed")
    after = _identity(restored)
    mismatches = compare_identity(before, after)
    if mismatches:
        raise SystemExit("restore identity mismatch: %s" % ",".join(mismatches))
    return {"ok": True, "size": before["size"], "root": before["root"], "src": str(src), "restored": str(restored)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PQ log backup/restore identity drill")
    parser.add_argument("--src", required=True, help="Throwaway pq-log sqlite (not /data)")
    parser.add_argument("--dest-dir", required=True, help="Throwaway snapshot directory (not /data)")
    args = parser.parse_args(argv)
    src = Path(args.src)
    dest_dir = Path(args.dest_dir)
    if not src.is_file():
        print("src missing")
        return 1
    try:
        result = run_drill(src, dest_dir)
    except SystemExit as exc:
        print(str(exc) or "drill failed")
        return 1
    print("ok size=%s root=%s" % (result["size"], result["root"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
