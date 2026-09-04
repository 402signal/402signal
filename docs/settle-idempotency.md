# Settle idempotency (SEC-ROUTER-001)

Process-local sqlite ledger for `/route` settle replay. Single-machine
until a shared ledger exists.

## What is stored

`UNIQUE` constraint on `SHA-256(fingerprint)`. Version 2 fingerprints bind
the rail-specific economic authorization: EIP-3009/Permit2 fields on Base,
the signed Solana message without mutable signatures, or Algorand unsigned
transaction IDs plus `paymentIndex`. Unsigned resource metadata, wrapper
extensions, and signature encoding do not create a fresh authorization.
Never the fingerprint, never `PAYMENT-SIGNATURE`, never raw payment material.
The uniqueness key is authorization-only; a separate hashed endpoint scope
prevents a cached `/route` result from being returned as an `/mcp` result.
A scope mismatch still fails closed and never creates a second economic action.

## Outcome states

| State | Terminal? | Second settle? |
|---|---|---|
| `settlement_pending` | no | no |
| `unknown` | no | no |
| `settled` | yes | no |
| `not_settled` | yes | no |
| `rejected` | yes | no |

`settlement_pending` is the reservation written at `begin()`. `unknown`
is a crash, `abandon`, lost/malformed settle response, or unreadable outcome.
The public unknown result uses `settled:null`; it does not assert a failed
settlement. Non-terminal states fail closed: no cached success, no second
economic action and no reuse of that authorization.

`not_settled` is a verified authorization whose route workflow ended in a
normal free miss. It durably replays the original safe response without a
second verification, probe workflow, or settlement attempt. New outcomes
use the strict `success_only_v1` billing object to distinguish `settled`
from `not_settled`; legacy stored 200/503 outcomes retain their prior
status-code classification.

## Version 1 ledger cutover

Legacy rows contain only irreversible hashes of the full client wrapper.
Their underlying authorizations and metadata variants cannot be reconstructed.
Exact legacy-wrapper replays remain readable and are sanitized on output, but
the ledger refuses every new version 2 reservation while any legacy row exists.

An operator may set `LIVE402_REPLAY_V2_CUTOVER_ACK` to the exact value
`payto-rotated-or-legacy-authorizations-expired` only after rotating every
advertised `payTo` or proving that every legacy authorization has expired.
The acknowledgement is persisted in ledger metadata. Without that explicit
safe cutover, deployment readiness stays false; deleting or rewriting legacy
history is not a migration strategy.

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
