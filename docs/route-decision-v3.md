# 402signal.route_decision.v3

New paid `/route` receipts use `402signal.route_decision.v3`. Historical
`route_decision.v1` and `route_decision.v2` leaves are not rewritten and
stay verifiable with their original semantics.

This is metadata minimization, not anonymity. Do not call v3 leaves
anonymous or unlinkable.

## Public leaf (logged)

Only these fields:

- `type` = `402signal.route_decision.v3`
- `ts` = RFC3339 UTC floored to the minute
- `nonce` = 32 random bytes, hex
- `commitment` = 32-byte SHA-256 hex

The public log does not receive private evidence or the salt.

Not on the public leaf: need, url, wallet, payTo, network, amount, asset,
outcome, live, miss_reason, identity, auth, seller body, salt.

## Private evidence (customer reveal only)

Deterministic canonical object, `evidence_version` 1, RFC 8785 JCS.
Missing facts are `null`. Catalog claims are never stored as observed.

Bound:

- request: `need`, `url`
- policy: `objective`, `constraints` (applied), `unresolved`
- decision: `outcome`, `winner_url`, `miss_reason`
- observation: `live`, `challenge_observed`, `payable`, `invocable`,
  `http_status`, `latency_ms`, `observed_at` (current probe, not catalog)
- selected_payment: `rail`, `network`, `scheme`, `asset`, `amount_atomic`,
  `payTo` (current observed option only)
- comparison: `candidate_count`, `candidate_set_digest`, `probe_batch_id`,
  `observation_batch_hash`
- scoring: `model_id`, `model_hash`

Not bound:

- catalog claimed fields
- raw PAYMENT authorization
- signatures
- customer wallet
- facilitator tokens
- seller response bodies
- API credentials
- the full `compared[]` array (only the candidate-set digest)
- the salt (concatenated after the canonical evidence)

## Candidate-set digest

If `compared` exists, SHA-256 of the JCS of a sorted slim list of those
rows (`url`, `rail`, `live`, `invocable`, `selected`, `amount_atomic`,
`latency_ms`, and selected_payment identity). The digest is private
evidence. The public leaf does not list candidates.

## Commitment

Cryptographically random 32-byte salt.

`SHA-256("402signal.route_decision.v3\0" || JCS(private_evidence) || salt)`

Customer reveal: private evidence, salt hex, expected commitment, event
version, plus public `ts` and `nonce` so `verify_route_receipt()` can
rebuild the leaf without the log store.

`verify_reveal_v3()` and `verify_route_receipt()` fail closed on missing
or mutated fields.
