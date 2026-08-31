#!/usr/bin/env python3
"""Bounded shadow-catalog measurements. Streams upserts; never builds a 44k list."""

from __future__ import annotations

import os
import resource
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("LIVE402_FIXTURE", "1")


def main() -> int:
    from live402 import catalog, shadow

    n = int(os.environ.get("LIVE402_BENCH_N") or 400)
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["LIVE402_CATALOG_DB"] = path
    shadow.reset()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    page = []
    for i in range(n):
        page.append(
            catalog.slim_item(
                {
                    "url": "https://bench.example/w/%d" % i,
                    "description": "hourly weather forecast station %d" % i,
                    "serviceName": "Wx %d" % i,
                    "tags": ["weather", "forecast"],
                    "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
                    "_input_schema_present": True,
                    "capability": "travel.weather",
                },
                "base",
            )
        )
        if len(page) >= 50:
            shadow.upsert_items(page, source="cdp")
            page.clear()
    if page:
        shadow.upsert_items(page, source="cdp")
        page.clear()
    ingest_s = time.perf_counter() - t0
    st = shadow.stats()
    latencies = []
    for _ in range(20):
        t1 = time.perf_counter()
        hits = shadow.fts_search("weather")
        latencies.append((time.perf_counter() - t1) * 1000.0)
    latencies.sort()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    extra = int(st["bytes"] * (44000 / float(n)))
    print(
        {
            "n": n,
            "sqlite_bytes": st["bytes"],
            "extrapolated_44k_mb": round(extra / (1024 * 1024), 2),
            "ingest_s": round(ingest_s, 3),
            "fts_hits": len(hits),
            "fts_p50_ms": round(latencies[len(latencies) // 2], 3),
            "ru_maxrss_kb": rss1,
            "ru_maxrss_delta_kb": rss1 - rss0,
            "path": path,
        }
    )
    shadow.reset()
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
