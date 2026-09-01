# Pre-key closeout (router). READY_FOR_PRODUCTION_KEY_INSTALL = NO

This PR is the public-router closeout before any production Falcon
key install. It does not Fly, does not set secrets, does not enable
MAINNET_BROADCAST or MAINNET_CANARY, does not fund, and does not
submit a MainNet transaction.

`READY_FOR_PRODUCTION_KEY_INSTALL` stays **NO** until 402security GO
on the exact SHA **and** `independent_provider=true` after cutover
review. Runtime may report a different-org confirm provider. The
committed `trust_root.v2` validator still expects pre-GO state
(`not_mainnet_go=true`, `independent_provider=false`).

## KEEP

- Tile bound `2^63-1` with HTTP 404 before SQLite
- Local deterministic Algorand txid
- MainNet-only signer path (`signer_mainnet.py`; no TestNet fallback)
- Durable one-shot canary (persist SEND_ATTEMPTED, then one POST)
- Independent-confirm abstraction (org map, not hostname)
- Canonical fee / fv / lv binding
- CodeQL Python + JavaScript
- No secrets, no Fly, no MainNet txn

## A. Fee: one size rule

Signer and router both use the same deterministic Falcon-1024
authorized-envelope estimate: max pk 1793, max compressed sig 1423.

Flow: canonical unsigned -> deterministic Falcon authorized-size
estimate -> canonical fee -> sign that exact txn.

Router equality: `txn.fee == required_fee(frozen policy, unsigned=decoded
unsigned txn)` using that same estimate. Never `len(signed)` for
MainNet equality. Prefer a small conservative overpayment over
disagreement. `MAX_FEE=30000`.

Fee integers 3000..30000 share the same 3-byte msgpack uint encoding,
so the estimate is stable across the allowed fee range.

## B. MainNet fv/lv truly canonical

`canonical_validity(..., require_canonical=True)` never accepts an
`fv=1` fallback. A trusted snapshot with `lastRound` is required.
Missing or invalid lastRound fails closed.

Policy: `fv = frozen_snapshot.lastRound`, `lv = fv + 1000`.
`1000` is Algorand `MaxTxnLife` and the reviewed 402Signal PQ1 span.
Do not invent a wider window.

MainNet validation is exact: `actual fv == policy.fv` AND
`actual lv == policy.lv`. The ±10 lookback/lookahead is a secondary
safety bound only.

Max acceptable age for a frozen suggested-params snapshot before
canary POST: `SNAPSHOT_MAX_AGE_S = 90`.

## C. Frozen policy snapshot

Router and signer agree on min fee, fee/byte, lastRound, fv, lv, and
canonical fee without public caller control. Values come only from
the trusted MainNet network-parameter path.

No `pq-anchor/1` protocol bump in this PR. The router stores a
narrow local fee-policy object (min_fee, fee_per_byte, last_round,
fv, lv, canonical_fee, snapshot_at, size_rule). Unknown extra
semantic fields are not HMAC-bound into the signer request.

Follow-up for 402security / private signer
`402signal/402signal-pq-signer` branch
`cursor/isolated-falcon-signer-9f06` @ `a901ef7a` (reviewed head
`9798c38f`): keep using the shared deterministic estimate plus the
signer's own trusted MainNet suggested-params snapshot. Bump
pq-anchor/1 only if extra authenticated policy fields become
unavoidable. Prefer solving on the router first.

Do not loosen fee/fv validation to paper over timing differences.

## D. Local txid official parity

`txid_from_unsigned` / `txid_from_signed` match official
`py-algorand-sdk` 2.11.1 `PaymentTxn.get_txid()` on baked vectors
(ordinary MainNet, MainNet PQ1, TestNet fixture, canary fee/fv/lv).
Txid is of the unsigned txn payload, not the Falcon sig hash.
`py-algorand-sdk` is not added to `requirements.txt`.

## E. Crash safety

Persist **before** the irreversible POST: exact SignedTxn bytes,
expected txid, origin, tree, root, fee-policy snapshot, fv, lv,
`state=SEND_ATTEMPTED`. Then POST only that exact stored blob.

After SEND_ATTEMPTED or a lost response: never ask the signer for
another txn, never advance fv/lv, never rebuild, never create
another auth, never auto-spend another fee, never auto-POST again.
First query the expected txid.

Human recovery (not implemented; security-approved later) may
retransmit the exact same SignedTxn/txid while the validity window
is still open. Validity expired and unresolved: stop for the
operator. Provider txid must equal the local expected txid.
Mismatch is a SECURITY FAILURE.

## F / item 6. Independent confirm (option B)

`LIVE402_PQ_CONFIRM_PROVIDER=tatum|nownodes`. Env chooses the enum
and the secret only. No env-chosen scheme, hostname, path, query, or
auth-header-name.

| role | name | org | host | path | auth header | secret env |
|---|---|---|---|---|---|---|
| PRIMARY | tatum | tatum | algorand-mainnet-indexer.gateway.tatum.io | /v2/transactions/{txid} | x-api-key | LIVE402_PQ_CONFIRM_TATUM_API_KEY (alias LIVE402_PQ_CONFIRM_INDEXER_TOKEN) |
| FAILOVER | nownodes | nownodes | algo-index.nownodes.io | /v2/transactions/{txid} | api-key | LIVE402_PQ_CONFIRM_NOWNODES_API_KEY |

NowNodes host is the documented Algorand Indexer base
(`https://docs.nownodes.io/algo/indexer.html`). Do not add
Blockdaemon. Confirm enum is tatum|nownodes only.

Always HTTPS, no redirects, bounded response, credentials only in
headers. Never in URL, query, logs, monitor snapshot, exception
text, trust root, or public response.

Excluded as independent (same org / AlgoNode-backed): AlgoNode
(`*.algonode.*`), Nodely (`*.4160.nodely.dev` / `.io`), Allo
explorer (`allo.info`; AlgoNode-developed per Algorand Foundation),
Oanor (`*.oanor.com`; documents live reads from public AlgoNode
APIs). A different Nodely hostname is not a different org.

Computed `confirmation_policy.independent_provider` is true when
confirm is Tatum or NowNodes and submit is AlgoNode/Nodely. Default
repo / committed `trust_root.v2` still has both hosts Nodely, so
`independent_provider` stays false there.

Production MainNet GO requires the Tatum (or NowNodes) allowlisted
confirm host plus the API key as a **Fly secret later**. Do not set
those secrets in this PR.

Org labels come from public company records, not a legal audit.

Confirm semantics unchanged: GET actual txn, decode locally,
semantic verify. Never HTTP 200 == confirmed. Never returned txid
alone. Never submit-provider pending as independent confirm.

## G. Split independence vs readiness

Flags: `confirm_provider_known`, `confirm_org_independent`,
`confirm_credentials_configured`, `confirm_reachable`,
`confirm_falcon_compatible`, `confirmation_ready`.

`runtime_confirmation_independent()` alone does not authorize
MainNet GO. Unknown provider/org is false. Missing credential or
failed probe is not ready.

## H. Tatum Falcon pqsig BLOCKER

This environment has no Tatum or NowNodes API key. A GET to the
exact Tatum production indexer
`https://algorand-mainnet-indexer.gateway.tatum.io/v2/transactions?sig-type=pqsig&limit=1`
without a key returned HTTP 500 (statement timeout). The public
AlgoNode MainNet indexer `sig-type=pqsig` search also timed out and
returned 0 transactions.

BLOCKER: Falcon `f1` fields (scheme, pk, sig, salt) are not proven
on the Tatum production endpoint. `CONFIRM_FALCON_COMPATIBLE` stays
false for both table entries. `confirmation_ready` stays false.
The decoder is not weakened. Do not treat Tatum as a production
confirm authority until a redacted fixture exists that
`decode_chain_txn` -> `verify_fetched_anchor` accepts.

## I. Trust root stays pre-GO

`LIVE402_PQ_CONFIRM_PROVIDER=tatum` does not rewrite committed
`trust_root.v2`. Runtime/ops may report different-org configured.
`validate_descriptor_v2` still requires `independent_provider=false`
and `not_mainnet_go=true`.

## J. Tile store boundary

Normalize once: `safe_n = check_tile_index(int(n))` (same for width).
Use `safe_n` / `safe_w` in SQLite. Oversized public paths are HTTP
404. Direct store tests cover MAX, MAX+1, huge int, huge grouped
path. No SQLite OverflowError.

## K. Code quality

`network.py` has singular final defs of `provider_org`,
`confirmation_independent`, `confirm_host_allowlisted`,
`runtime_confirmation_independent`, `confirmation_status`. Tests
import those final functions.

## Residuals (do not claim YES)

- No Fly secrets
- No Falcon SK install
- No funding
- No MainNet txn
- No cutover
- Tatum/NowNodes Falcon pqsig unproven
- `confirmation_ready=false`
- `trust_root.v2.not_mainnet_go=true`
- `trust_root.v2.confirmation_policy.independent_provider=false` (default hosts still Nodely)
- Confirm API keys are Fly secrets later, not in this PR
- Org independence is from public company records, not a legal audit
- `READY_FOR_PRODUCTION_KEY_INSTALL=NO`
