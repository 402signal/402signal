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
| `not_settled` | yes | no |
| `rejected` | yes | no |

`settlement_pending` is the reservation written at `begin()`. `unknown`
is a crash, `abandon`, or unreadable outcome. Non-terminal states fail
closed: no cached success, no second economic action.

`not_settled` is a verified authorization whose route workflow ended in a
normal free miss. It durably replays the original safe response without a
second verification, probe workflow, or settlement attempt. New outcomes
use the strict `success_only_v1` billing object to distinguish `settled`
from `not_settled`; legacy stored 200/503 outcomes retain their prior
status-code classification.

TTL (120s) expires the in-memory response cache only. Sqlite uniqueness
does not expire.

## Scope

One process tree, one sqlite file. A second machine with its own file
is not covered until a shared ledger exists. This does not claim
facilitator exactly-once.

Production requires `/data/live402-replay.sqlite`; `/ready` is false when that
durable ledger is unavailable. Test support may use an isolated temp path.
The WAL uses `synchronous=FULL` so a successful reservation commit is not
silently lost on host power failure.
