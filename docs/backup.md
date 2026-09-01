# SQLite backup and restore

Tooling is implemented. Scheduled Fly backups are **not** claimed active.

402Signal keeps process-local SQLite files on the `/data` volume:

- catalog (claims)
- history (observed probes)
- pq-log (historical TestNet archive; not the live production log)
- pq-log-mainnet (live production MainNet epoch `mainnet-v1`; do not copy TestNet leaves into it)

The live production file is `/data/pq-log-mainnet.sqlite`. The TestNet archive `/data/pq-log.sqlite` stays in place. Do not delete it. Do not copy TestNet leaves into the MainNet file.

One machine. One writer. Do not attach a second process to the same files.

## Local / operator tooling

```bash
PYTHONPATH=. python3 scripts/backup_sqlite.py --dest /tmp/402signal-backup
PYTHONPATH=. python3 scripts/restore_sqlite.py --src /tmp/402signal-backup/catalog-….sqlite --dest /data/catalog.sqlite --force
```

`backup_sqlite.py` uses the SQLite backup API (an online snapshot). It does not rewrite leaves or mutate v1 events.

Restore only after the app process is stopped. `--force` replaces the destination file and drops leftover `-wal`/`-shm`.

## Fly volume snapshot (human checklist)

Fly scheduled snapshots are off in `fly.toml`. An admin must do this by hand:

1. Confirm `min_machines_running = 1` and no second machine writing `/data`.
2. `fly volumes list -a 402signal`
3. `fly volumes snapshots create <volume-id> -a 402signal`
4. `fly volumes snapshots list <volume-id> -a 402signal`
5. Store the snapshot id and time off-box.
6. Restore only with Fly volume restore docs for that snapshot. Do not `fly ssh` delete live sqlite to "test" restore.
7. After restore, start one machine and check `GET /health` then `GET /ready`.
8. Do not set `LIVE402_PQ_FALCON_BROADCAST` as part of backup/restore.

Until a scheduled snapshot policy is enabled by an admin, backups are tooling plus this checklist only.

## Isolated TestNet restore drill (no Fly secrets)

Use a copy of `pq-log.sqlite` on a workstation. Do not use production Fly secrets, Falcon keys, or MainNet hosts.

1. `LIVE402_PQ_LOG_DB=/path/to/pq-log.sqlite PYTHONPATH=. python3 scripts/backup_sqlite.py --dest /tmp/402signal-testnet-drill`
2. Confirm the snapshot `pq-log-*.sqlite` exists and `PRAGMA integrity_check` is `ok`.
3. Restore to a throwaway path: `PYTHONPATH=. python3 scripts/restore_sqlite.py --src /tmp/402signal-testnet-drill/pq-log-….sqlite --dest /tmp/pq-log-restored.sqlite --force`
4. Open the restored file read-only and compare tree size, Merkle root, and checkpoint notes against the source (`scripts/pq_log_restore_drill.py`).
5. Leave `/data/pq-log.sqlite` untouched. Do not restore over the live file in this drill.
6. Do not set `LIVE402_PQ_FALCON_BROADCAST` or `LIVE402_PQ_FALCON_MAINNET_BROADCAST`.

See `docs/pq-recovery.md` for drills A-F.
