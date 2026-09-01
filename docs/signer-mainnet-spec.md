# 402signal-pq-signer-mainnet spec

Preferred new app name: `402signal-pq-signer-mainnet`.

This public router repository does not contain the isolated signer and
must not reimplement it. The live TestNet signer stays
`402signal-pq-signer` (pq-anchor/1). MainNet uses a new app, a new HMAC
token name, and a new 6PN hostname.

The signer authorizes only. It never broadcasts. It never reads
`LIVE402_PQ_FALCON_BROADCAST` or `LIVE402_PQ_FALCON_MAINNET_BROADCAST`.
Destroying the Falcon key is not the kill switch.

github.com/402signal/402signal-pq-signer was not readable from this
environment. Implement the signer in that private repo against this
spec. Do not add Falcon SK handling to 402signal.

## Identity

| Field | Value |
|---|---|
| App | `402signal-pq-signer-mainnet` |
| 6PN host | `402signal-pq-signer-mainnet.internal` |
| Port | `9091` (never 8080) |
| Protocol | TCP JSON-line, one object per connection, newline-terminated |
| HMAC token env (router) | `LIVE402_PQ_SIGNER_MAINNET_TOKEN` |
| HMAC token env (signer) | same name on the signer machine; value never committed |
| Falcon scheme | `f1` (Falcon-1024) |
| Network | Algorand MainNet only |
| Genesis ID | `mainnet-v1.0` |
| Genesis hash | `wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=` |
| Checkpoint origin | `402signal.com/pq/log/mainnet-v1` |
| Address env | `LIVE402_PQ_FALCON_MAINNET_ADDRESS` (public, empty until Ross ceremony) |

Do not reuse the TestNet HMAC token. Do not reuse the TestNet Falcon
secret. Do not reuse the TestNet Ed25519 log secret.

## Reconstruction

The router sends exactly these JSON keys (same as pq-anchor/1):

`v`, `origin`, `tree_size`, `root`, `consistency`, `timestamp`,
`request_id`, `checkpoint`, `hmac`

`checkpoint` is the Ed25519 signed-note the log already produced. The
signer reconstructs the unsigned PaymentTxn from trusted semantic
fields only:

- type `pay`
- amount `0`
- sender = receiver = configured MainNet Falcon f1 address
- note = PQ1 84-byte note from origin, tree_size, root
- genesis ID + hash = MainNet exact values
- fee = derived Falcon required value (not a router-supplied field)

Fee formula (same as the router; 402security must review):

```
required = max(fee_per_byte * deterministic_falcon_envelope_estimate, protocol_base_min * 3)
```

ONE SIZE RULE: both signer and router use the same deterministic
Falcon-1024 authorized-envelope estimate (official max pk 1793, max
compressed sig 1423). Never mix `len(signed)` with that estimate.
Never derive a smaller fee from a shorter actual Falcon sig.

Protocol base min is 1000 µAlgo today. Falcon-1024 adds 2x that base
(uncongested floor 3000). algod suggested `fee` is fee per byte.
Validity: `fv = trusted lastRound`, `lv = fv + 1000` (MaxTxnLife,
reviewed 402Signal policy). Missing lastRound fails closed. No
`fv=1` fallback on MainNet. The signer authorizes that exact
canonical txn. If required > 30000, reject. Do not raise the cap.
Do not hardcode fee=3000 forever. Caller cannot select the fee.

Do not accept router-supplied fee, firstValid, sender, amount, or an
unsigned txn blob. Unknown JSON keys are rejected.

## Fee cap

`MAX_FEE = 30000` microAlgos. Fail closed if the derived Falcon
required fee exceeds the cap. The signer may read suggested params
from an allowlisted MainNet algod host to learn `min-fee` and
fee-per-byte. It still does not POST.

## Rejection matrix

Reject (no SignedTxn) when any of these hold:

| Reason | Detail |
|---|---|
| Wrong origin | not `402signal.com/pq/log/mainnet-v1` |
| Wrong genesis | not MainNet ID+hash |
| Amount nonzero | must be 0 |
| Sender != receiver | must be self-pay |
| Address mismatch | not the configured MainNet Falcon f1 |
| Note mismatch | origin/tree/root/format/version |
| Rekey / close / lease / group | any nonempty |
| AuthAddr / sgnr | any nonempty |
| Other txn types | axfer, appl, acfg, afrz, keyreg, stpf |
| LogicSig / multisig / ordinary sig | exclusive sig keys |
| Scheme not `f1` | including f5 or missing pqsig |
| Fee > 30000 | cap |
| Unknown request keys | including unsigned txn fields |
| HMAC fail | token mismatch or stale timestamp |
| Checkpoint unsigned | not a C2SP signed-note |

## State

The signer does **not** own AUTHORIZED / SUBMITTED / CONFIRMED. Those
live on the router log:

- AUTHORIZED: signer returned a SignedTxn (router persists)
- SUBMITTED: router POSTed (signer never does this)
- CONFIRMED: router fetch+decode+verify of the actual txn

Public status is CONFIRMED only. Paid `/route` never waits for chain.

The signer is **not** fully stateless except for the loaded Falcon key
and HMAC token. It MUST retain durable **security** state across
restarts:

- monotonic checkpoint progression
- last-authorized identity (origin, tree_size, root, signed-note) for
  conflict detection
- replay / request-ID tracking
- freshness (timestamp window)
- HMAC verification
- checkpoint signature verify
- origin / tree / root binding
- consistency validation
- reject a conflicting authorization for the same progression
- safe restart: after authorizing origin=X tree=N root=R, a restart
  must not authorize X/N/R2 or an unsafe rollback
- bounded request body
- rate limit
- unknown-field reject

This is a spec, a contract, and signer-repo tests only. Do not
reimplement the private signer inside this public router.

## Tests the signer repo must have

- Reconstructs MainNet pay-0 self-Falcon f1 from origin/tree/root only
- Rejects the full matrix above
- Never reads either broadcast env
- Never POSTs to algod
- Fixture/CI never hits live MainNet
- Token unset: refuse to start or refuse to sign
- Distinct token from TestNet: TestNet token must not validate
- Durable security state survives restart: no X/N/R2, no unsafe rollback
- Rejects conflicting auth for the same progression
- Bounded body, rate limit, unknown-field reject
- Fee equals derived Falcon required (not router-supplied)

## Kill switch

Unset `LIVE402_PQ_FALCON_MAINNET_BROADCAST` on the router, or do not
deploy the signer. Routing and the log still work. Do not destroy the
Falcon key as the kill switch.
