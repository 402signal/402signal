# Automatic MainNet Falcon anchoring

This controller is exact-opt-in and default-off. Merging or deploying the
code does not enable signing or broadcast. It does not run on `POST /route`,
so paid routing never waits for an anchor.

## Policy

- Eligible after at least one unanchored leaf has existed for 15 minutes,
  or immediately at 1000 new leaves.
- Maximum fee per anchor: 30000 microAlgos (0.03 ALGO).
- Maximum automatic fee spend: 500000 microAlgos per UTC day and
  10000000 microAlgos per UTC month.
- Maximum 12 automatic anchor attempts per UTC hour.
- Warn below a 5 ALGO Falcon balance. Stop before signing below 1 ALGO.
- Maximum one automatic pre-POST re-sign for a stale policy snapshot.
- MainNet uses pq-anchor/3 only. The response HMAC and exact SignedTxn
  semantic checks are required before AUTHORIZED persistence.

The controller freezes one tree identity before signer IPC. New leaves may
continue arriving, but the frozen root remains valid and commits its prefix.
The next eligible anchor covers later leaves.

## Durable state machine

`AUTHORIZED -> SEND_ATTEMPTED -> SUBMITTED -> CONFIRMED`

The exact signer-returned SignedTxn bytes are persisted once in the existing
authorized anchor table. The controller derives the txid locally. In one
SQLite transaction it reserves the fee budget and records SEND_ATTEMPTED
before the only network POST. After that latch, every tick fetches and
verifies that exact txid only. Timeout, transport error, invalid response,
process crash, or ambiguous POST never causes a second POST or re-sign.

A process lock avoids duplicate work inside one router. SQLite uniqueness
and the atomic reservation are the authoritative cross-process guard: a
losing process cannot reach POST. Production still uses one writer and one
attached volume.

## Volume and log efficiency

- The observation row is committed only when tree or confirmation state
  changes, not on each five-second worker wakeup.
- No leaves, tiles, checkpoints, or SignedTxn blobs are duplicated.
- Automation jobs store compact policy metadata; a replaced pre-POST
  SignedTxn is represented by its SHA-256 digest only.
- Confirmed automation job and fee-accounting rows older than 40 days are
  pruned. Existing confirmed and authorized anchor audit records remain.
- Repeated blocked-state logs are limited to one per condition and tree per
  15 minutes.
- Failed provider, policy, or budget preflight is retried at most once per
  minute in-process. The one identical signer-response recovery call and,
  after SEND_ATTEMPTED, exact-txid confirmation are attempted at most once per
  10 seconds in-process. A restart may make one early retry but cannot create
  a second POST.
- Do not create backup archives on `/data`. Stream verified backups to an
  external destination so backup retention does not consume the live volume.

SQLite reuses deleted pages. Do not run automatic `VACUUM` on the live
single-writer database because it can block routing and temporarily require
additional disk space.

## One-time activation

Deployment and activation are separate reviewed operations. The repository
and `fly.toml` intentionally contain none of the activation flags.

1. Deploy with AUTO, AUTO_KILL, BROADCAST, and CANARY unset. Confirm `/health`,
   `/ready`, current tree/root, and public confirmed anchor are unchanged.
2. Confirm the deployed signer is pq-anchor/3-only and its health/ready checks
   pass. Do not sign during this preflight.
3. Confirm there is no unresolved AUTHORIZED, SEND_ATTEMPTED, SUBMITTED, or
   HALTED automation job. Never clear such state to make activation pass.
4. Confirm an independent Tatum or NowNodes confirmation provider is selected
   and its credential is installed. The controller re-fetches and fully
   verifies the latest confirmed Falcon anchor before creating a new job.
5. Confirm the Falcon balance is at least 1 ALGO and that no unexpected
   account activity exists.
6. Keep `LIVE402_PQ_FALCON_MAINNET_CANARY` unset. Set exact
   `LIVE402_PQ_FALCON_MAINNET_AUTO=1` and
   `LIVE402_PQ_FALCON_MAINNET_BROADCAST=1`. Keep AUTO_KILL unset.
7. Confirm the public trust descriptor reports automatic `on` and the
   documented limits. Observe the first automatic cycle through CONFIRMED.

The app currently runs a single machine. Any Fly secret or release change may
restart that machine, so the operator must use the existing reviewed release
procedure and live health checks. This source PR performs no deployment or
secret change.

## Kill and recovery

Set exact `LIVE402_PQ_FALCON_MAINNET_AUTO_KILL=1` to stop automatic signing
and POST. Routing, leaf append, receipts, and read APIs continue. Also removing
the broadcast capability blocks POST, but the dedicated kill flag makes
intent visible.

Never wipe or reset the router or signer, change keys, reset counters, discard
an ambiguous attempt, or re-POST. If state is SEND_ATTEMPTED or SUBMITTED,
preserve it and poll only the exact derived txid. A HALTED job requires human
incident review. Key rotation, restore, incident recovery, deployment,
activation, and account replenishment remain human actions by design.

## Backup boundary

Automatic anchoring is the recurring economic action this change removes.
Database backup destination, encryption, retention, restore drills, and Fly
deployment remain operator responsibilities. `scripts/backup_sqlite.py`
provides an online SQLite snapshot, but a production backup is not complete
until the verified output is transferred off the live volume to a separately
controlled destination. Provider credentials and a destination must not be
invented or committed by this controller.
