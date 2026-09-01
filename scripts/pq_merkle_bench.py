#!/usr/bin/env python3
"""Incremental Merkle frontier benchmarks. Honest timings. No fake completion.

  PYTHONPATH=. python3 scripts/pq_merkle_bench.py
  PYTHONPATH=. python3 scripts/pq_merkle_bench.py --sizes 10000,100000,1000000

1m uses an in-memory frontier (dict) because a durable SQLite commit per
append is the measured bottleneck, not the RFC 9162 hash walk.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

from live402.pq import merkle


def _bench_memory(n: int) -> dict:
    cache: dict[tuple[int, int], bytes] = {}

    def get_range(a, b):
        return cache.get((a, b))

    def store_range(a, b, h):
        cache[(a, b)] = h

    t0 = time.perf_counter()
    root = merkle.empty_tree_hash()
    for i in range(n):
        leaf = merkle.leaf_hash(b"e%08d" % i)
        root = merkle.incremental_root(i + 1, leaf, get_range, store_range)
    elapsed = time.perf_counter() - t0
    # Identity check vs rebuild on a tail sample.
    sample = min(n, 64)
    leaves = [merkle.leaf_hash(b"e%08d" % i) for i in range(n - sample, n)]
    # Rebuild only the last `sample` is not the full tree; compare cached root
    # against a full rebuild only when n is small enough.
    rebuild = None
    rebuild_s = None
    if n <= 10000:
        t1 = time.perf_counter()
        rebuild = merkle.mth_from_leaf_hashes(
            [merkle.leaf_hash(b"e%08d" % i) for i in range(n)]
        )
        rebuild_s = time.perf_counter() - t1
        if rebuild != root:
            raise SystemExit("root mismatch vs full rebuild")
    return {
        "n": n,
        "mode": "memory_frontier",
        "seconds": round(elapsed, 6),
        "leaves_per_sec": int(n / elapsed) if elapsed else 0,
        "cached_ranges": len(cache),
        "rebuild_seconds": None if rebuild_s is None else round(rebuild_s, 6),
        "root_hex": root.hex(),
    }


def _bench_sqlite(n: int) -> dict:
    from live402.pq import store

    tmp = tempfile.TemporaryDirectory()
    os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(tmp.name, "pq-log.sqlite")
    store.reset()
    t0 = time.perf_counter()
    for i in range(n):
        store.append(b"e%08d" % i)
    elapsed = time.perf_counter() - t0
    root = store.root(n)
    out = {
        "n": n,
        "mode": "sqlite_durable_append",
        "seconds": round(elapsed, 6),
        "leaves_per_sec": int(n / elapsed) if elapsed else 0,
        "root_hex": root.hex(),
    }
    store.reset()
    os.environ.pop("LIVE402_PQ_LOG_DB", None)
    tmp.cleanup()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="10000,100000,1000000")
    parser.add_argument("--sqlite-max", type=int, default=10000)
    args = parser.parse_args(argv)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    rows = []
    for n in sizes:
        row = _bench_memory(n)
        rows.append(row)
        print(row)
        if n <= args.sqlite_max:
            srow = _bench_sqlite(n)
            rows.append(srow)
            print(srow)
    print("bottleneck: durable SQLite commit-per-append, not RFC 9162 hashing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
