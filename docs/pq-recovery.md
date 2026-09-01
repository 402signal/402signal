# PQ recovery drills A-F

These drills keep AUTHORIZED, SUBMITTED, and CONFIRMED distinct. Public
status is CONFIRMED only. Paid `/route` never waits for chain.

Automated fixture tests live in `tests/test_pq_recovery_drills.py`.
They do not use the MainNet network. Do not set either Falcon
broadcast flag to perform a live drill. Do not submit a MainNet
transaction.

## A. Isolated TestNet backup and restore

See `docs/backup.md`. Snapshot `pq-log.sqlite` with
`scripts/backup_sqlite.py`, restore to a throwaway path, compare tree
size, Merkle root, and checkpoint notes
(`scripts/pq_log_restore_drill.py`). Leave `/data/pq-log.sqlite` in
place. No Fly secrets.

## B. Authorized, not submitted

A signer reply persists AUTHORIZED (request_id + SignedTxn). If the
router restarts before POST:

- Recover the exact blob when size, origin, root, and signed-note match
- Do not re-dial the signer
- Do not mark CONFIRMED
- TestNet POST still requires `LIVE402_PQ_FALCON_BROADCAST=1`

## C. Submitted, not confirmed

If AUTHORIZED already has `submitted=True` and a real txid:

- Skip POST
- Poll that txid through the confirm provider
- Persist CONFIRMED only after fetch+decode+verify of the actual txn
- HTTP 200 or a returned txid is not confirmation

## D. Authorized mismatch fail-closed

If the stored authorized row disagrees on size, origin, root, or
signed-note:

- Do not re-dial
- Do not reuse the old SignedTxn
- Do not overwrite
- Wait for operator review

## E. Fresh MainNet identity

After cutover prep:

- TestNet tree N stays N on `/data/pq-log.sqlite`
- MainNet tree starts at 0 on `/data/pq-log-mainnet.sqlite`
- Distinct Ed25519 vkey and origin `402signal.com/pq/log/mainnet-v1`
- First append yields tree size 1
- No TestNet state is migrated

## F. Misconfig (never send)

These combinations must never send a MainNet transaction:

1. `LIVE402_PQ_FALCON_BROADCAST=1` + `LIVE402_PQ_FALCON_NETWORK=mainnet`
   + MainNet flag unset
2. MainNet flag `=1` + `LIVE402_PQ_FALCON_NETWORK=testnet`
3. Fixture/CI (`LIVE402_FIXTURE=1`)
4. Missing MainNet Falcon address, signer token, genesis, checkpoint,
   allowlisted host, or fee-within-cap

Kill switch: unset the MainNet flag, or do not deploy. Routing and the
log still work. Do not destroy the Falcon key.
