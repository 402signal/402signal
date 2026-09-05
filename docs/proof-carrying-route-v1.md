# Proof-carrying routes v1

This opt-in addition binds a qualifying route to the request and payment terms
402Signal observed. The buyer can compare a fresh seller challenge with signed
evidence immediately before its own signing operation.

Send `require_route_binding: true` in the existing `/route` request. Existing
requests continue to receive v3 receipts. This flag also requires a signed
checkpoint receipt; it does not wait for an Algorand transaction.

The routing fee remains **$0.003 USDC only when a valid live route is found**.
Normal typed misses are not settled. Seller payment is separate. If the binding
cannot be built before settlement, the result is a 503 `constraints_unmet` with
`binding_error: route_binding_unavailable`, and a durable free-miss replay result.
If settlement succeeds and the required receipt subsequently fails, the result
is 503 with **billing.settled=true**. `unavailable` does not prove that no leaf
was appended; v4 never attempts a second append to repair a failed receipt.
Never retry payment to repair that outcome.

## Contract and scope

`decision_binding` has exactly these fields:

| Field | Meaning |
|---|---|
| `model` | `proof_carrying_route_v1` |
| `observed_at` | Unix seconds when the actual challenge was received |
| `expires_at` | Observation time plus the configured freshness window |
| `request.url` | Exact HTTPS URL, including query, without redirects |
| `request.method` | Actual GET or POST used by the probe |
| `request.body_sha256` | SHA-256 of the exact probe request body bytes |
| `quote_sha256` | SHA-256 of the entire strictly parsed x402 v2 envelope |
| `selected_index` | Unique selected option in that envelope's `accepts` array |

The full-envelope hash includes resource metadata, all accepts (in order),
facilitator/fee-payer data in `extra`, and supported extension data. Object key
order does not matter; strings and arrays are exact. No loose URL or address
normalization is performed. The profile supports current `exact` v2 options on
Base, Solana and Algorand; unknown top-level fields and protocol extensions other
than `bazaar` fail closed. Opaque `extra` data is bound without asserting its
transaction semantics. The existing official rail validator/wallet must still
validate all actual payment effects before signing.

The default freshness window is 60 seconds. `LIVE402_ROUTE_BINDING_TTL_S` accepts
integers 1..120; invalid settings fail closed for opted-in requests. Receipt
issuance, HTTP retries, and replay never reset observation time. The x402
`maxTimeoutSeconds` field is an authorization timeout, not a quote expiry and is
not used to invent one. Expiry can occur while settling or waiting for approval;
an expired receipt is unusable, and the original billing result remains accurate.

Current probes send GET without a body, or a justified POST with exactly `{}`.
The guard accepts only that same URL, method and body. It does not certify an
arbitrary input merely because a schema exists. Routes requiring a different
POST body, redirects, personalized/rotating challenges, unsupported extensions,
or unresolved natural-language policy may be ineligible. There is no fallback to
ordinary unguarded execution. Optional binding availability is narrower than
ordinary routing availability.

## Authentication and privacy

`402signal.route_decision.v4` uses evidence version 2:

```
SHA256("402signal.route_decision.v4\0" || canonical(evidence) || random_32_byte_salt)
```

`evidence` has exactly `evidence_version`, `binding`, `request_json`, and
`routing_evidence_json`. The latter two are JSON **strings**. Preserve their exact
bytes when verifying the outer commitment. This deliberately avoids changing the
historical v3 numeric serialization or requiring Python and JavaScript to
re-serialize legacy floating-point evidence identically. Decode the strings to
inspect the policy, winner, observation, selected payment, candidate digest and
scoring model. They are never executable instructions.

The outer commitment uses an RFC8785 subset: null, booleans, Unicode strings,
arrays, objects, and safe integers only. Reject floats, non-finite numbers,
duplicate keys, lone surrogates, unsafe integers, excessive size/depth, unknown
binding/evidence fields, and unsupported versions. Public leaves still contain
only `type`, minute-rounded `ts`, nonce and salted commitment. The producer does not copy seller response bodies or buyer payment headers,
keys, authorizations or wallet signatures into this new evidence. Retain the private receipt/reveal securely, outside public
logs. The private SQLite replay ledger retains completed response objects,
including private reveals; its in-memory cache TTL is not a durable deletion
policy. No new retention or deletion mechanism is added. Do not include
credentials in routing prompts, policy, or resource URLs.

Before comparing the seller challenge, verify the reveal/commitment, public leaf
hash, Merkle inclusion, Ed25519 checkpoint signature, and expected log origin with
an **independently pinned log verification key**. A key from the same untrusted
response is not a trust anchor. The response's `decision_binding` must equal the
authenticated binding. Compare `request_json` with the actual route request the
buyer made, not a request supplied by an agent after the fact. Policy comparison
uses JSON value semantics: numeric 1 and 1.0 are equivalent, booleans are never
coerced into numbers. This does not re-serialize the signed commitment strings.

The immediate proof is the existing Ed25519 log checkpoint. A later cumulative
Algorand Falcon anchor is separate evidence; pending never means confirmed. The
guard does not claim the immediate receipt or Base/Solana payment is PQ-secure.
v1-v3 historical leaves and their original verification paths remain unchanged.

## Python verifier

For Node/TypeScript, see the [local route guard](../sdk/route-guard/README.md).
It uses the same signed fixtures and a caller-owned authorization callback, with
no runtime dependencies or network operations. It is distributed as source in
this repository and is not published to npm. See the
[developer walkthrough](https://402signal.com/developers#route-binding) for the
request and buyer-side integration sequence.

```python
from live402.route_binding import observed_challenge, verify_route

envelope = observed_challenge(seller_status, lower_case_headers, seller_body)
accepted = verify_route(
    route_response, actual_route_request, vkey=pinned_log_vkey,
    status=seller_status, envelope=envelope,
    url=actual_seller_url, method=actual_seller_method,
    body=actual_seller_request_bytes,
)
# Pass accepted to your existing payment validator/signer under your own policy.
```

Supply the actual response and exact request bytes from the buyer's HTTP client.
Do not follow a redirect or change the body after this check. The verifier itself
performs no network requests. A comparison-only `verify_challenge` helper is also
available; it does not authenticate a receipt and is not the public trust boundary.

## Limits of the claim

This proves that the current supplied challenge and request match terms that
402Signal recorded for a qualifying route. It does not guarantee delivery, output
quality, immutable future seller behavior, legality, identity, merchant intent,
or safety of every transaction an arbitrary wallet might construct. It cannot
force a compromised buyer runtime to use the guard. The caller still enforces
wallet policy, signature scope and economic replay protections. Recheck after a
human delay; an approval is never permission to ignore expiry or changed terms.

No AC2 connection, approval queue, signing service, key, infrastructure component,
or extra on-chain transaction is introduced. The existing batched anchor lifecycle
and limits remain in force. There are no additional server probes for binding.

## Validation and rollback

Run the existing complete fixture suite and `test_route_binding.py` separately.
`scripts/route_binding_vectors.py` reproduces all-rail signed conformance fixtures
using a public test key and a temporary log. Python and the client must agree on
them, including trees of different sizes. No live payment is needed.

Deploy only after review. Rollout is caller opt-in. To stop new v4 issuance,
clients can stop requesting the flag; retain v4 verification for already issued
receipts. Rolling back code does not undo a settlement and must not wipe or reset
replay, history, signer, or transparency state. Never silently turn a request for
guarded execution into an unguarded payment.
