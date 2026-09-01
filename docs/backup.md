# SQLite backup and restore

Tooling is implemented. Scheduled Fly backups are **not** claimed active.

402Signal keeps three process-local SQLite files on the `/data` volume:

- catalog (claims)
- history (observed probes)
- pq-log (append-only transparency log)

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
