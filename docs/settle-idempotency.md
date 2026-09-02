# Settle idempotency (SEC-ROUTER-001)

Process-local sqlite ledger for `/route` settle replay. Single-machine
until a shared ledger exists.

## What is stored

`UNIQUE` constraint on `SHA-256(fingerprint)`. Never the fingerprint,
never `PAYMENT-SIGNATURE`, never raw payment material.

## Outcome states

| State | Terminal? | Second settle? |
|---|---|---|
| `settlement_pending` | no | no |
| `unknown` | no | no |
| `settled` | yes | no |
| `rejected` | yes | no |

`settlement_pending` is the reservation written at `begin()`. `unknown`
is a crash, `abandon`, or unreadable outcome. Non-terminal states fail
closed: no cached success, no second economic action.

TTL (120s) expires the in-memory response cache only. Sqlite uniqueness
does not expire.

## Scope

One process tree, one sqlite file. A second machine with its own file
is not covered until a shared ledger exists. This does not claim
facilitator exactly-once.

Default path: `/data/live402-replay.sqlite` when `/data` is writable,
else `/tmp/live402-replay.sqlite`, or `LIVE402_REPLAY_DB`.
