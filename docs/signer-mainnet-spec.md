# 402signal-pq-signer-mainnet spec

Preferred new app name: `402signal-pq-signer-mainnet`.

This public router repository does not contain the isolated signer and
must not reimplement it. The live TestNet signer stays
`402signal-pq-signer` (pq-anchor/1). MainNet uses a new app, a new HMAC
token name, a new 6PN hostname, and **pq-anchor/3**.

The signer authorizes only. It never broadcasts. It never reads
`LIVE402_PQ_FALCON_BROADCAST` or `LIVE402_PQ_FALCON_MAINNET_BROADCAST`.
Destroying the Falcon key is not the kill switch.

The isolated signer lives in the Ross-only private repository. Keep its
source and secrets there; do not add Falcon SK handling to 402signal.

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
| Wire protocol | `pq-anchor/3` (JSON `v=3`; request MAC `v=pq-anchor/3`) |

Do not reuse the TestNet HMAC token. Do not reuse the TestNet Falcon
secret. Do not reuse the TestNet Ed25519 log secret. Do not accept a
pq-anchor/1 MainNet request.

## pq-anchor/3 request

The router sends exactly these JSON keys:

`v`, `origin`, `tree_size`, `root`, `consistency`, `timestamp`,
`request_id`, `checkpoint`, `policy`, `hmac`

`v` is `3`. `checkpoint` is the Ed25519 signed-note the log already
produced. `policy` is the **narrow frozen snapshot object only**:

```
canonical_fee, fee_per_byte, fv, last_round, lv, min_fee,
size_rule, size_version, snapshot_at
```

`size_rule` is exactly `deterministic_falcon_envelope_estimate`.
JSON `size_version` is the string `"1"` (Go `SizeVersion` is a
string). The HMAC flatten is still `size_version=1`. The router
rejects int `1` at `narrow_policy` (no int-to-str coerce).

Router serialization gate (offline, no signer credentials):
`tests/test_pq_cross_repo_wire.py`. The immutable
`tests/fixtures/pq_anchor2_wire/` corpus remains only as migration
evidence; active requests are v3-only. Full Go parser and response-MAC
integration runs on private signer CI under Ross. Do not add a GitHub
secret on this public router for signer access.
Do **not** send an arbitrary txn, unsigned blob, fee, firstValid,
sender, amount, pk, or sk as top-level keys. Unknown JSON keys are
rejected.

### HMAC canonical bytes (flat; matches Go signer)

The MAC does **not** nest `policy=canonical_fee=…,fee_per_byte=…`.
Policy fields are flattened into the same sorted `k=v` lines as the
identity fields (same algorithm as pq-anchor/1: version line, then
sorted keys, each `k=v\\n`).

Exact field order (`sorted` ASCII; `live402.pq.signer_mainnet.CANONICAL_KEYS`):

```
canonical_fee
checkpoint
consistency
fee_per_byte
fv
last_round
lv
min_fee
origin
request_id
root
size_rule
size_version
snapshot_at
timestamp
tree_size
v
```

```
pq-anchor/3\n
canonical_fee=<decimal>\n
checkpoint=<signed-note>\n
consistency=<hex nodes joined by comma>\n
fee_per_byte=<decimal>\n
fv=<decimal>\n
last_round=<decimal>\n
lv=<decimal>\n
min_fee=<decimal>\n
origin=<origin>\n
request_id=<id>\n
root=<lowercase hex>\n
size_rule=deterministic_falcon_envelope_estimate\n
size_version=1\n
snapshot_at=<decimal unix seconds>\n
timestamp=<decimal>\n
tree_size=<decimal>\n
v=pq-anchor/3\n
```

The request preimage remains exactly 380 UTF-8 bytes for the published
golden inputs. Router test `tests/test_pq_prekey_correction.py`
(`FLAT_HMAC_V3_GOLDEN`) matches that preimage byte-for-byte and retains
the v2 golden separately as migration evidence. MAC `v` is the protocol
id string `pq-anchor/3`, not the JSON integer `3`.

Integers are decimal with no leading zeros (except the value `0`).
`hmac` is hex(HMAC-SHA256(token, canonical)). Golden vector:
`tests/test_pq_prekey_correction.py` (`FLAT_HMAC_V3_GOLDEN`). Share that
exact UTF-8 byte string with the private signer repo.

Invalid HMAC must reject with **exactly**:

```
{"ok":false,"error":"hmac"}
```

That is the preferred reviewed-signer probe reply. A well-formed
pq-anchor/3 request with an invalid HMAC must never produce a
SignedTxn. Router preflight treats `error=hmac` (and the small
exact allowlist containing only `hmac`) as the request-auth boundary.
A success reply to an invalid HMAC is a protocol failure.

## pq-anchor/3 success response

The signer returns exactly these JSON keys on success:

`ok`, `tree_size`, `root`, `pqsig`, `signed`, `response_hmac`

`ok` is the JSON boolean `true`; `tree_size` is a JSON integer (never a
boolean); `root`, `signed`, and `response_hmac` are canonical lowercase
hex; and `pqsig` is exactly `present`. Duplicate, missing, or unknown
keys fail closed. The router preserves the exact decoded SignedTxn bytes.

`response_hmac` is HMAC-SHA256 with the same MainNet IPC token over a
domain-separated, length-prefixed byte string. It binds, in this exact
order: `checkpoint`, `origin`, `pqsig`, the original request `hmac`,
`request_id`, request protocol `pq-anchor/3`, `root`, the exact raw
SignedTxn bytes, and `tree_size`. The domain is
`pq-anchor-response/1`. Length prefixes make embedded newlines and
binary SignedTxn bytes unambiguous. The router verifies this MAC before
identity binding, semantic SignedTxn validation, or AUTHORIZED
persistence. There is no pq-anchor/2 fallback.

## Reconstruction

The signer reconstructs the unsigned PaymentTxn from trusted semantic
fields plus the **HMAC-bound policy**:

- type `pay`
- amount `0`
- sender = receiver = configured MainNet Falcon f1 address
- note = PQ1 84-byte note from origin, tree_size, root
- genesis ID + hash = MainNet exact values
- fee = `policy.canonical_fee` (must equal independently derived required)
- fv = `policy.fv` = `policy.last_round`
- lv = `policy.lv` = `fv + 1000`

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
Validity: `fv = frozen last_round`, `lv = fv + 1000` (MaxTxnLife,
reviewed 402Signal policy). Missing lastRound fails closed. No
`fv=1` fallback on MainNet. Once the signer **accepts** the router
snapshot, it signs that **exact** HMAC-bound policy. It never
silently modifies fee, fv, or lv. If required > 30000, reject. Do
not raise the cap. Do not hardcode fee=3000 forever. Caller cannot
select the fee.

## Independent MainNet params (required)

The signer MUST independently `GET /v2/transactions/params` from an
approved hardcoded MainNet algod allowlist (HTTPS, no redirects,
bounded body/timeout, exact MainNet genesis). No caller-controlled
URL, fee, fv, or lv.

Before signing, the signer MUST validate the router snapshot:

| Check | Rule |
|---|---|
| Network | MainNet only (`mainnet-v1.0` + MainNet genesis hash) |
| Freshness | router `snapshot_at` within `SNAPSHOT_MAX_AGE_S = 90` |
| lastRound slack | signer-observed last-round not more than `LAST_ROUND_SLACK = 10` ahead or behind `policy.last_round` |
| min-fee | observed min-fee valid (≥ 1) |
| Required fee | if signer current required fee **>** frozen `policy.canonical_fee` → **reject** and require a **NEW router snapshot** before auth |
| Equality | after accept, sign EXACT HMAC-bound fee/fv/lv. Never rewrite them |

Slightly different observation times must either agree on the
authenticated frozen policy **or** fail closed before AUTHORIZED.
Do not loosen canonical validation to paper over timing.

Suggested-params allowlist (same as the router reader):

- `mainnet-api.algonode.cloud` (primary)
- `mainnet-api.4160.nodely.dev` (secondary)

Path is always `/v2/transactions/params`. The signer still does not POST.

## Fee cap

`MAX_FEE = 30000` microAlgos. Fail closed if the derived Falcon
required fee exceeds the cap.

## Rejection matrix

Reject (no SignedTxn) when any of these hold:

| Reason | Detail |
|---|---|
| Wrong protocol | not pq-anchor/3 / `v!=3` / missing `policy` |
| size_version | not exactly `1` |
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
| Policy vs observed | freshness / lastRound slack / required fee > frozen |
| Unknown request keys | including unsigned txn fields |
| HMAC fail | token mismatch or stale timestamp; reply `{"ok":false,"error":"hmac"}` |
| Checkpoint unsigned | not a C2SP signed-note |

## State

The signer does **not** own AUTHORIZED / SUBMITTED / CONFIRMED. Those
live on the router log:

- AUTHORIZED: router persists only after the pq-anchor/3 response HMAC
  authenticates the exact SignedTxn bytes and full request identity,
  followed by strict semantic SignedTxn validation. Caller-supplied
  SignedTxn bytes cannot reach persistence. Fixture `sign_fn` /
  `LIVE402_FIXTURE` may persist only through a separate test capability.
  Already-confirmed checkpoints and read-only trust are unchanged.
- SEND_ATTEMPTED: router latched expected txid before POST
- SUBMITTED: router POSTed (signer never does this)
- CONFIRMED: router fetch+decode+verify of the actual txn

Public status is CONFIRMED only. Paid `/route` never waits for chain.

The signer is **not** fully stateless except for the loaded Falcon key
and HMAC token. It MUST retain durable **security** state across
restarts:

- monotonic checkpoint progression
- last-authorized identity (origin, tree_size, root, signed-note, **and HMAC-bound policy digest**) for conflict detection
- replay / request-ID tracking
- freshness (timestamp window)
- HMAC verification
- checkpoint signature verify
- origin / tree / root binding
- consistency validation
- reject a conflicting authorization for the same progression
- explicit operator re-auth of the same X/N/R with a **new** HMAC-bound policy is allowed only before the router has SEND_ATTEMPTED (router discard is local). Signer must still refuse unsafe rollback and refuse a second spend for an already-consumed progression. Prefer reject-replay if the previous policy is still within its validity window
- safe restart: after authorizing origin=X tree=N root=R, a restart
  must not authorize X/N/R2 or an unsafe rollback
- bounded request body
- rate limit
- unknown-field reject

This is a spec, a contract, and signer-repo tests only. Do not
reimplement the private signer inside this public router.

## Paired private-signer requirements

Private repo: `402signalhq/402signal-pq-signer` (historical personal path `402signal/402signal-pq-signer` redirects)
The public router contains only the client, protocol contract, and
mocks. Signer source and secrets remain private. The paired signer
change must:

1. Add **pq-anchor/3** to the MainNet app and authenticate every v3
   success response. The router speaks v3 only. The signer may retain v2
   temporarily for deployment compatibility, but must reject pq-anchor/1;
   remove v2 after the v3 router rollout is verified.
2. HMAC-verify flattened policy fields (`size_version=1` required) using the canonical encoding above. Do not nest `policy=` in the MAC.
3. Independently fetch MainNet `/v2/transactions/params` from the hardcoded allowlist.
4. Apply freshness / lastRound slack / min-fee / required-fee-vs-frozen rules. Never rewrite fee/fv/lv.
5. On accept, sign the **exact** HMAC-bound policy. Router verifies equality.
6. Invalid HMAC → `{"ok":false,"error":"hmac"}`. No SignedTxn.
7. Keep durable monotonic / replay / conflict state. No automatic second spend.
8. Never read BROADCAST/CANARY. Never POST. Fixture/CI never hit live MainNet.
9. Add tests: two observation times agree **or** fail closed; required fee > frozen rejects; policy field missing rejects; unsigned txn keys reject.
10. Authenticate every successful response with the exact
    `pq-anchor-response/1` contract above, including the original request
    HMAC and exact raw SignedTxn bytes. Never MAC a re-encoded transaction.

## Tests the signer repo must have

- Reconstructs MainNet pay-0 self-Falcon f1 from origin/tree/root + HMAC policy
- Signs exact frozen fee/fv/lv after accept
- Rejects the full matrix above
- Independently fetched params; required fee > frozen → reject
- lastRound slack exceeded → reject
- Stale router snapshot_at → reject
- Never reads either broadcast env
- Never POSTs to algod
- Fixture/CI never hits live MainNet
- Token unset: refuse to start or refuse to sign
- Distinct token from TestNet: TestNet token must not validate
- Durable security state survives restart: no X/N/R2, no unsafe rollback
- Rejects conflicting auth for the same progression
- Bounded body, rate limit, unknown-field reject
- Invalid HMAC returns exact `error=hmac`
- Fee equals derived Falcon required (not a top-level router fee field)

## Kill switch

Unset `LIVE402_PQ_FALCON_MAINNET_BROADCAST` on the router, or do not
deploy the signer. Routing and the log still work. Do not destroy the
Falcon key as the kill switch.
