# TestNet PQ log archival runbook (read-only)

TEST SUPPORT / archive only. The TestNet log is construction history.
It is not the production MainNet tree. Do not copy leaves into
`/data/pq-log-mainnet.sqlite`. Do not delete `/data/pq-log.sqlite`.
Production never falls back to this shard.

This runbook is read-only. It records what to archive before a later
MainNet cutover. It does not fund, broadcast, or rotate keys.

## What to keep

Archive these artifacts off-box. Public values only.

| Item | Where | Notes |
|---|---|---|
| TestNet sqlite | `/data/pq-log.sqlite` (plus `-wal` / `-shm` if present) | Online snapshot via `scripts/backup_sqlite.py`. Do not HTTP-serve the file. |
| Trust descriptor | `GET /pq/log/trust` and `live402/pq/trust_root.v1.json` | Version 1. TestNet only. No private key. |
| Ed25519 verifier key | `LIVE402_PQ_LOG_VKEY` / `GET /pq/log/trust` `log_signature.vkey` | Public. Never archive `LIVE402_PQ_LOG_SK`. |
| Falcon public address | `LIVE402_PQ_FALCON_ADDRESS` (TEST SUPPORT) | Archived TestNet f1 account. Not the production MainNet address. |
| Latest signed checkpoint | `GET /pq/log/checkpoint` | C2SP signed-note. May be newer than last confirmed. |
| Confirmed history | `confirmed_anchors` rows / `GET /transparency` | CONFIRMED only. AUTHORIZED and SUBMITTED are not public status. |
| Tree size and root | `store.size()` and RFC 9162 root at that size | Empty tree root is the RFC empty hash. |
| Software SHAs | Deploy git SHA used for the image (`GIT_SHA` build-arg) | Record the Fly image / git SHA that produced the archive. Do not add a public SHA endpoint. |

## Snapshot command (no Fly secrets)

```bash
LIVE402_PQ_LOG_DB=/data/pq-log.sqlite \
  PYTHONPATH=. python3 scripts/backup_sqlite.py --dest /tmp/402signal-testnet-archive
```

Store the `pq-log-*.sqlite` file, the trust JSON, the public vkey, the
Falcon address, the latest checkpoint text, a dump of confirmed
txid/round/size/root rows, and the software SHA together.

## Checks after the snapshot

1. `PRAGMA integrity_check` is `ok`.
2. `SELECT COUNT(*) FROM leaves` matches the recorded tree size.
3. The latest checkpoint origin is `402signal.com/pq/log` (TestNet).
4. Confirmed rows still verify as pay-0 self-Falcon f1 PQ1 notes.
5. `/data/pq-log.sqlite` is still on the volume. Nothing was deleted.

## What this archive is not

- Not a seed for `/data/pq-log-mainnet.sqlite` (see
  `docs/runbooks/mainnet-prelaunch-reset.md` for the empty MainNet reset)
- Not permission to set MainNet broadcast
- Not a MainNet homepage claim
- Not a substitute for the later Ed25519 and Falcon ceremonies
