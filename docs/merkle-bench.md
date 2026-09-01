# Incremental Merkle frontier benchmarks

Honest timings from `PYTHONPATH=. python3 scripts/pq_merkle_bench.py --sizes 10000,100000,1000000 --sqlite-max 10000` after the v3/settlement closeout. No fake completion.

RFC 9162 / C2SP hashing is unchanged. Historical roots match a full rebuild (verified at 10k).

| n | mode | seconds | leaves/sec |
|---|---|---|---|
| 10,000 | memory frontier | 0.089 | 112,488 |
| 10,000 | SQLite durable append | 128.468 | 77 |
| 100,000 | memory frontier | 1.274 | 78,485 |
| 1,000,000 | memory frontier | 17.097 | 58,489 |

1m was run in memory. A durable SQLite commit-per-append at the measured 10k rate (~77 leaves/s) would be about 3.6 hours for 1m leaves. That is the bottleneck: `store.append` commits WAL after every leaf (tiles/bundles publish included). The RFC 9162 walk itself is O(log n) per append and is not the limiter.

Do not claim a fully efficient 1m durable append from this change. 1m durable was not measured.
