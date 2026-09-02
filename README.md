# 402Signal

402Signal is the independent check an AI agent makes right before spending. It finds the strongest x402 route across Base, Solana, and Algorand, verifies it live, and shows the evidence behind the choice. Your agent keeps the wallet. The decision history is committed to an append-only PQ Trust log. Production identity is Algorand MainNet. Falcon authorizes a checkpoint transaction, not a merchant payment. This is not a PQ payment rail and not a claim that the product is fully quantum-proof.

- **Live site:** https://402signal.com
- **Paid API:** `POST /route` — $0.01 USDC
- **MCP:** https://402signal.com/mcp.json
- **OpenAPI:** https://402signal.com/openapi.json

Dated 2026-08-29. Production verify + settle. No private payment keys. We never pay upstream.

## Run locally

Python 3.12 (stdlib plus pinned `cryptography` for Coinbase CDP JWTs and Ed25519 log signatures). Local default is **127.0.0.1:8081**. Fly / Docker bind **0.0.0.0:$PORT** (default 8080).

```bash
PYTHONPATH=. python3 -m live402
```

Then open http://127.0.0.1:8081

Unpaid `POST /route` returns HTTP 402. Local operator loop (skip the paywall, still probe):

```bash
LOCAL_FREE=1 PYTHONPATH=. python3 -m live402
```

`LOCAL_FREE=1` is tests-only. Production must not set it.

Offline / tests (no network, fixture catalog):

```bash
LIVE402_FIXTURE=1 LOCAL_FREE=1 PYTHONPATH=. python3 -m live402
```

Tests:

```bash
LIVE402_FIXTURE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## POST /route

Body:

```json
{ "need": "erc20 token balance", "url": "https://example.com/x402/balance", "prefer_network": "base" }

`need` or `url` (or both) is required.
```

- No valid payment and `LOCAL_FREE` unset → **HTTP 402**. One 402 lists three accepts (Base, Solana, Algorand) plus the bazaar extension. We do not probe.
- Valid payment: verify with the matching facilitator, then probe, then settle. An unverified header never opens the gate.
- If `url` is set: must be `https`. Unknown public URLs are port 443 only; catalog-known listings may use the HTTPS port already present on that listing. Unpaid probe is GET first, then POST `{}` only when GET is 405/501 AND the catalog explicitly declares POST AND does not require a request body. Never POST `{}` after GET 200/400/401/403/404/500. Never POST seller-declared or catalog-declared input bodies. If a required body means a valid unpaid probe cannot be constructed, the typed miss is `unsafe_to_probe`. DNS uses a bounded resolver pool (`getaddrinfo`, 2s); the TCP/TLS connection is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host set to the original hostname (re-checked and re-pinned on each redirect hop). Fail closed if pin/SNI/Host cannot be applied. ~4s timeout. Response includes `live`, `status`, `latency_ms`, `has_402_challenge`, `selected_payment`, and a `health` snapshot. 402Signal settles the $0.01 routing payment; it does not pay the selected merchant.
- If only `need`: federated need-scoped search (local FTS on `catalog.sqlite` + Coinbase / PayAI / GoPlausible) at request time (CDP `/discovery/search`; PayAI / GoPlausible search or a small first-pages fetch), union/dedupe/rank that working set, hydrate only the top ~5–10 finalists with claimed method/schema/toolName (bounded, TTL, optional gzip on disk; never a 44k RAM schema index), then probe adaptively (first 3, expand 2–4 if no winner; hard ceiling 20). Return the best currently observed live option (not the first live URL, not a catalog-only rail). A HTTP 200 winner is a resource plus the exact observed payment option that made it win: `selected_payment` is never null, never copied from a catalog claim, and when `networks` is set its `network` must be in that lock. Live hits are write-through upserted onto disk. Catalog claims stay on `claimed.payment_options` and `catalog.sqlite` `accept_claims`; `target.accepts` and selection use the current HTTP 402 envelope only. Claimed schemas are not observed payment options. If none live → **HTTP 503** `{ "live": false, "tried": n }` after settle (they paid for an honest miss). Optional structured constraints are top-level request fields: `max_price_usd`, `max_total_cost_usd` (merchant + known fees; unknown fee fails closed), `max_probe_latency_ms`, `max_service_latency_ms` (historical p50, not probe RTT), `max_settlement_latency_ms` (settlement/finality, not probe RTT), `require_invocable`, `networks` (hard policy lock), `min_observations`, `min_observed_success`, `min_reputation_score`, `min_reputation_confidence`. A nested `constraints` object is rejected rather than silently ignored. `prefer_network` is a weak ranking preference only; it does not become a filter. `max_latency_ms` is a probe-RTT alias. Unknown measured values fail closed. Optional `objective`: `best` / `cheapest` / `fastest` / `most_reliable` / `lowest_total_cost` / `fastest_settlement`. `cheapest`, `fastest`, and `most_reliable` rank the currently probed eligible candidates, not every discovered endpoint. `fastest` is this-request probe RTT, not settlement latency (`fastest_settlement` stays separate). Optional `policy` / need phrasing such as `weather under $0.01 and 300ms` compiles to structured constraints; "established usage" / "strong observed evidence" compile to `min_observations=10`; vague "high reputation" stays unresolved; settlement / total-cost language compiles only with a numeric bound. The engine uses structured values only. `interpreted_constraints` / `applied_constraints` echo constraints actually used. Paid `/route` and unpaid `/preview` include transparent `reputation` components (observed / usage / tenure / stability / source_count) plus V1 `reputation_score`, `reputation_confidence`, and `scoring_model_id` / `scoring_model_hash`. Rail `economics` (merchant price, fees, settlement/finality) sit on the selected payment option and on `compared[]`, each field labeled `402signal_observed`, `protocol_reference`, or `unknown`. Same model on Base, Solana, and Algorand (no algo bonus). Catalog is not a 0–100 badge. Pulse stays facts; rates stay hidden below `n=10`. Unique payer addresses are never listed. Most usage/settlement/unique-payer fields are unknown on first ship because 402Signal has probe history, not a settlement ledger.
- Dead upstream is **503** with the snapshot, never a fake live URL.
- `LIVE402_FIXTURE=1` uses `live402/data/fixtures.json`. No network.
- Paid **HTTP 200** includes `target: { method, inputSchema, outputSchema, accepts, facilitator, amountAtomic, displayAmount, timeoutSeconds }` (envelope accepts only) and `selected_payment: { rail, network, asset, amount_atomic, display_amount, normalized_usd, payTo, facilitator }` for the exact observed option that won. `payable` requires a complete observed option; `invocable` is payable plus input schema. If schema is missing, `live` may still be true with `invocable: false` and `miss_reason: "no_input_schema"`. `accepts[].extra.facilitator` is copied as `{url, feePayer, caip2, scheme}` — do not default to x402.org.
- `miss_reason` is a closed enum: `no_candidates`, `no_402_envelope`, `no_payto`, `reachable_200`, `probe_timeout`, `quote_expired`, `invalid_need`, `upstream_5xx`, `ssrf`, `no_input_schema`, `constraints_unmet`, `probe_budget_exhausted`, `probe_limit_reached`, `unsafe_to_probe`. HTTP 402 with no usable payTo is `no_payto` (typed miss, not retry-pay). Probe budget is under 60s; a hang returns **503** JSON immediately. `stop_reason` and `probe_ceiling` say why probing stopped.
- `GET /health` is **HTTP 200** `{ "ok": true }` for Fly checks. Not a paid listing. Not a rails dump.
- `GET /ready` checks storage, catalog, history, and pq_log. Response is booleans only (no paths, no secrets). Fly health stays on `/health` until `/ready` is proven safe in staging.
- `GET /preview?need=` is an unpaid request-time catalog search (`not_probed: true`, hits + prices + freshness + facilitator/method/inputSchema_present/rails_up, optional `also_on[]`). Optional `prefer_network=base|solana|algorand` is a weak ranking preference: it ranks that rail first but still searches all three. Optional `networks=solana` (repeat or comma-separate) is a hard policy lock on which rails are queried. `discovery_via` is a compact per-rail how-found map; `discovery_exhaustive` is true only when the returned set is known complete. It does not probe and does not charge. Paid `POST /route` remains the fail-closed 402 probe.
- `GET /rails` lists the three pay-in networks, asset, amountAtomic, facilitators, feePayers, maxTimeoutSeconds, and per-rail up+latency. Cached. Not stuffed into `/health`.
- `GET /pulse` is a JSON snapshot. Catalog totals stay unpublished. Discovery uses current upstream catalogs plus a process-local shadow (not a full-world RAM index). `index_status` is `upstream-live`, `shadow-warm`, `both`, or `fixture`. Observed `n_7d` comes from `402signal_observed`. Rates (`success_7d`, `payable_rate_7d`, `invocable_rate_7d`) are omitted below `n=10`. There is no binary `healthy` and no `executable_now_rate`. Query params are ignored — no caller-supplied URLs. Cached ~15s. Fail-open: never waits on a discovery crawl. The trickle refresher never blocks `/route`.

## Shadow catalog refresh queue

Background trickle is one bounded step at a time (a few stale URLs, or one COLD page). It does not rebuild a 44k RAM catalog and does not add network fanout beyond the existing discovery/probe budgets.

Priority (first matching reason wins; then `last_fetched` / URL). Same order in `live402/shadow.py` `REFRESH_REASONS`:

1. **recent_search** — searched in the last hour, claim older than `LIVE402_HOT_REFRESH_S`
2. **recent_route** — routed in the last hour, claim stale
3. **source_disagreement** — two catalogs disagree on amount or payTo for the same rail
4. **price_change** — recent `price_changed` claim event
5. **payto_change** — recent `payTo_changed` claim event
6. **schema_change** — recent `schema_changed` claim event
7. **failed_probe** — last independent probe was not live
8. **stale_observation** — never verified, or last verification older than a day
9. **high_demand_capability** — capability with at least two recently searched listings

If the queue is empty, the refresher takes one COLD generation page.
- `GET /dashboard` is the same samples as HTML. Per-chain lookups you can try; click through to prefill the homepage form. Also free.
- GET `/` homepage is plain English: one line on what `/route` is, humans pointed at free `GET /preview`, agents at POST / MCP. Footer is 402signal.com / @402Signal. Hidden Pay $0.01 on Base (injected wallet only) signs one EIP-3009 authorization and POSTs PAYMENT-SIGNATURE. Algorand and Solana stay agent/CLI. A short “for agents” box shows `POST https://402signal.com/route` plus links to `/llms.txt`, `/preview`, `/rails`, `/openapi.json`, `/.well-known/x402.json`, and `/mcp.json`. Nav is Home / Pulse (GET `/pulse`); no `/dashboard` in homepage nav.
- `GET /route` is split by `Accept`: browsers (`text/html`) get the human page (HTTP 200). Agents (`application/json`) and curl with no Accept get HTTP 402 + bazaar + accepts (amount 10000). Agents that intend to pay should **POST**, not GET.
- Discovery: `GET /openapi.json`, `GET /mcp.json`, `GET /.well-known/x402`, `GET /.well-known/x402.json`, `GET /robots.txt`, `GET /llms.txt`, `GET /preview`, `GET /rails`. Paid `POST /route` is documented with `x-payment-info` and HTTP 402. MCP bazaar type is `mcp` + `toolName: route`.
- `POST /validate` (also `GET /validate?url=`) is an unpaid seller probe: is this endpoint agent-ready? Only URLs already in the catalog or fixture are probed (no arbitrary public fetch). Same unpaid helper as `/route`: GET first, justified POST `{}` only, never a catalog-declared body, DNS IP-pin, fail-closed SSRF. Does not write `402signal_observed`. Not a `/route` payment bypass. Returns readiness, claimed vs observed, flags. Never a binary `healthy` flag.
- `GET /attestation` is a public sha256 of canonical JSON of a recent `402signal_observed` probe batch (`batch_id`, `created_at`, `n`, `algo`, `hash`). Not on-chain. No signatures or keys. Optional `?batch_id=`.
- `GET /pq/log/checkpoint` and `GET /pq/log/tile/*` are an **experimental** C2SP transparency log (tlog-checkpoint@v1.0.0 + tlog-tiles@v0.1.0). Production identity is MainNet-only: origin `402signal.com/pq/log/mainnet-v1`, epoch `mainnet-v1`, DB `/data/pq-log-mainnet.sqlite`, `LIVE402_PQ_FALCON_NETWORK=mainnet`, pq-anchor/3, MainNet Falcon address, and authenticated MainNet signer responses. Unset or unknown network fails closed. There is no live TestNet PQ fallback. TestNet constants, `LIVE402_PQ_LOG_SK`, `LIVE402_PQ_SIGNER_TOKEN`, `LIVE402_PQ_FALCON_BROADCAST`, `LIVE402_PQ_FALCON_ADDRESS`, `/data/pq-log.sqlite`, and pq-anchor/1 remain TEST SUPPORT (tests and archive) only. Automatic MainNet anchoring stays off. Worker, tick, and boot never auto-send. Production AUTHORIZED persistence requires a verified response HMAC over the exact SignedTxn bytes followed by strict semantic validation; caller-supplied bytes cannot reach persistence. Existing confirmed tree-4 evidence remains readable. AUTHORIZED / SEND_ATTEMPTED / SUBMITTED are never rendered as CONFIRMED. Explorer links follow independently recorded network/genesis. MainNet evidence never uses TestNet URLs. Falcon SK must never live on 402signal. The isolated signer never reads BROADCAST and never POSTs. `last_confirmed` is persisted only after an independent fetch+decode+verify. Signing or POST success is not confirmation. Paid `POST /route` does not wait for chain inclusion. Falcon authorizes a checkpoint transaction, not a merchant payment. Paid `POST /route` may include optional `pq_trust.transparency` `{status: pending|logged_uncheckpointed|unavailable, state, log_origin, index, checkpoint_size, receipt, reveal}`. Paid 200/503 is not atomic with log append (SEC-ROUTER-004 / A-14): those codes do not require a durable signed leaf unless `require_transparency` is true. `pending` means a durable leaf plus a signed checkpoint (state `checkpoint_signed`). `logged_uncheckpointed` means the leaf is durable without a signed checkpoint and is never success when `require_transparency` is true. `unavailable` means append failed (not pending). Never say signed if there is no checkpoint. Newly generated route receipts use `402signal.route_decision.v3`: the public leaf is type, minute-rounded ts, nonce, and sha256 commitment only. Historical v1 and v2 leaves stay verifiable with their original semantics. A public leaf is not a claim of anonymous or unlinkable traffic. The customer `reveal` holds private evidence, salt, expected commitment, and event version. `verify_route_receipt()` checks event version, reveal, commitment, leaf_hash, inclusion, and the Ed25519 checkpoint. `receipt.leaf_hash` is present so `verify_receipt()` can still round-trip. `payment_authorization.pq_native` is always false. No `/trust` page. The homepage PQ card renders only when `last_confirmed` has a real confirmed txid. Production Ed25519 signing uses `LIVE402_PQ_LOG_SK_MAINNET` only (never baked into git; never auto-generated on boot; never falls back to `LIVE402_PQ_LOG_SK`).
- `POST /route` is rate-limited in memory (~60/min per IP). User-Agent does not grant extra quota. On Fly the limiter key is `Fly-Client-IP`; otherwise the socket peer. `X-Forwarded-For` is not trusted. Rate-limit maps and per-host probe semaphores are TTL/LRU bounded. `GET /preview` and unpaid MCP `tools/call preview` share a looser limiter (~180/min per IP, at least 2× the route cap). `GET /pulse` and `GET /rails` each have their own ~180/min per-IP limiter (same ballpark as preview, still looser than paid `/route`). `GET /health` stays unlimited `{ok:true}`. `429` when exceeded. Production request logs are request id, method, path only, status, latency, and a coarse endpoint. Query strings, need, policy, `/preview` search, `/validate` target URL, request JSON, `PAYMENT-SIGNATURE`, `X-PAYMENT`, payment payloads, and seller response bodies are not logged. Settlement logs are `settlement_success=` plus rail and request id, not full txids. Responses send `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security: max-age=31536000` (no includeSubDomains; www is a CNAME and Fly has no extra hostnames), and `Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'`. script-src stays `'self'` (no CDN, no vendor wallet scripts). connect-src is `'self'` only; homepage Base pay POSTs `/route`. HEAD 200 on `/llms.txt` `/openapi.json` `/mcp.json` `/preview` `/rails` `/pulse`. Payment resource / OpenAPI `servers` / MCP resource are pinned to `https://402signal.com` (Host is not reflected). Probe DNS uses a bounded `getaddrinfo` pool (2s); the TCP/TLS connection is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host set to the original hostname (re-pinned on redirects). Seller-declared catalog bodies are never POSTed; unjustified POST `{}` is skipped; required-body misses are `unsafe_to_probe`. Paid `/route` uses one advertised-timeout deadline for verify, probe, and settle.

Clients send v2 `PAYMENT-SIGNATURE` (base64 `PaymentPayload`) or v1 `X-PAYMENT`. Success/settle echo is `PAYMENT-RESPONSE`.

## Reputation V1 and rail economics

Components come first. A score is never returned without them.

| Component | What it is | What it is not |
|---|---|---|
| observed | probe success count, n, distinct days, freshness, outcome stability | uptime, a health badge |
| usage | `402signal_observed` probe counts only | reputation, settlements (unknown — no ledger), unique payers (omitted — no identities) |
| tenure | first seen, days listed | quality |
| stability | payTo / price / schema / rail changes | a guarantee |
| source_count | independent catalog sources | popularity |

**V1 score** (0–1, chain-neutral, documented in `live402/reputation.py` and the sqlite `scoring_models` log):

- observed_performance **0.50** — the only reliability-like signal we measure. Popularity cannot dominate.
- stability **0.20** — recent identity/quote churn is a risk signal.
- tenure **0.10** — age ≠ quality. Log-capped at 365 days.
- usage **0.10** — log-capped probe counts (`log1p(n)/log1p(100)`). 0 probes and unknown usage are both dropped so 0 does not look worse than unknown. Settlements and unique payers are never faked from probes.
- distribution **0.10** — `min(source_count, 3) / 3`. Not in catalog ≠ 0 sources.

Missing components are dropped and lower `reputation_confidence`. `n_7d < 10` caps confidence at 0.35. No public reliability % below `n=10`. Same function on Base, Solana, and Algorand. No `algo_bonus`.

**Economics** (same keys on every rail, provenance on every field):

- `402signal_observed` — we measured it (merchant price from the current 402 option).
- `protocol_reference` — a cited official figure (Base L2 inclusion ~2s; Algorand block finality 2.82s). Solana wall-clock finality is unknown (no current official ms).
- `unknown` — missing. Chain fees and facilitator fees are unknown in USD (no FX oracle). `lowest_total_cost` / `max_total_cost_usd` fail closed. `fastest_settlement` / `max_settlement_latency_ms` use settlement or protocol finality, never probe RTT.

## Rails

| Rail | payTo | asset | network (402 body) | Facilitator verify / settle |
|---|---|---|---|---|
| Base | `0xb18fc2275f36dae99eb215caeff03b431f887d16` | USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | `base` (facilitator sees `eip155:8453`) | `https://api.cdp.coinbase.com/platform/v2/x402/verify` and `/settle` |
| Solana | `HCM423cyKYVUoq9GvmqUphZwYVB6M2wez34i9jzSewLy` | mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` | `https://facilitator.payai.network/verify` and `/settle` |
| Algorand | `N2JSJZCSORMYGYO2NSIYRUEMBFRHEOMYODVXV2MXYYHB5H2JVUGG6NJ4NQ` | ASA `31566704` | `algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=` | `https://facilitator.goplausible.xyz/verify` and `/settle` |

Amount is **$0.01 USDC** (`10000` atomic, 6 decimals). Bazaar is echoed on settle so catalogs can index.

Base CDP calls need `CDP_API_KEY_ID` + `CDP_API_KEY_SECRET` (or `CDP_ACCESS_TOKEN`). PayAI is free-tier without a key; optional `PAYAI_API_KEY`. GoPlausible needs no auth. Never put a wallet private key in env.

## Env

| Variable | Default | Meaning |
|---|---|---|
| `LIVE402_PORT` | `8081` | bind port (local) |
| `PORT` | unset | Fly / Docker port; if set, host defaults to `0.0.0.0` |
| `LIVE402_HOST` | `127.0.0.1` local / `0.0.0.0` when `PORT` is set | bind host |
| `PAYTO_ADDRESS` | Base payTo above | Base `payTo` |
| `PAYTO_SOLANA` | Solana payTo above | Solana `payTo` |
| `PAYTO_ALGORAND` | Algorand payTo above | Algorand `payTo` |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | unset | CDP JWT for Base verify/settle |
| `CDP_ACCESS_TOKEN` | unset | pre-minted CDP Bearer (optional) |
| `PAYAI_API_KEY` | unset | optional PayAI Bearer beyond the free tier |
| `LOCAL_FREE` | unset | `1` skips the paywall (tests only) |
| `LIVE402_FIXTURE` | unset | `1` uses local JSON, no network |
| `LIVE402_PROBE_TIMEOUT` | `4` | probe timeout seconds |
| `LIVE402_HISTORY_DB` | `/data/live402-history.sqlite` on Fly (`/tmp` fallback) | sqlite probe history (WAL, 0600, capped). Observed only. |
| `LIVE402_CATALOG_DB` | `/data/catalog.sqlite` on Fly (`/tmp` fallback) | sqlite shadow catalog of CDP/PayAI/GoPlausible **claims**. Process-local on the existing `/data` volume. **Not HTTP-exposed** (no dump/download endpoint, not under `static/`, not in OpenAPI). Separate file from history. FTS5. Never a 44k RAM list. |
| `LIVE402_PQ_LOG_DB` | `/data/pq-log-mainnet.sqlite` on Fly (`/tmp` fallback) | PRODUCTION C2SP log. Separate from catalog, history, and the TestNet archive `/data/pq-log.sqlite` (TEST SUPPORT only). **Not HTTP-exposed** as a sqlite dump; read API is `/pq/log/*` only. Ross-only empty reset: `docs/runbooks/mainnet-prelaunch-reset.md`. |
| `LIVE402_PQ_LOG_EPOCH` | `mainnet-v1` in fly.toml | PRODUCTION requires `mainnet-v1`. Unset/unknown fail closed. `testnet-v1` is TEST SUPPORT only. |
| `LIVE402_PQ_LOG_ORIGIN` | `402signal.com/pq/log/mainnet-v1` in fly.toml | PRODUCTION origin. TestNet origin is archive/TEST SUPPORT only. |
| `LIVE402_PQ_LOG_VKEY` | unset | TEST SUPPORT Ed25519 log verifier key (public). Production never uses this. Never a private key. |
| `LIVE402_PQ_LOG_VKEY_MAINNET` | unset | PRODUCTION Ed25519 log verifier key (public). If set with `LIVE402_PQ_LOG_SK_MAINNET`, must exactly match the C2SP vkey derived from that SK (fail closed; env is not overwritten). If SK is set and VKEY is unset, boot writes the derived vkey (ops may stage SK first). If SK is unset, there is no MainNet signer. Never a private key. |
| `LIVE402_PQ_LOG_SK` | unset | TEST SUPPORT Ed25519 seed only. Production never loads this. Never commit, never paste into chat. |
| `LIVE402_PQ_LOG_SK_MAINNET` | unset | PRODUCTION Ed25519 seed only. Code rejects silent fallback to `LIVE402_PQ_LOG_SK`. Install via stdin/file (`NAME=-`); never a CLI secret argument. Never set from this PR. |
| `LIVE402_PQ_FALCON_ADDRESS` | unset | TEST SUPPORT TestNet Falcon address. Not a production default. Never a private key. |
| `LIVE402_PQ_FALCON_NETWORK` | `mainnet` in fly.toml | PRODUCTION requires `mainnet`. Unset/unknown fail closed. `testnet` is TEST SUPPORT only. |
| `LIVE402_PQ_FALCON_BROADCAST` | unset | TEST SUPPORT 402signal (router) env. `1` allows POST of a signer-approved SignedTxn to pinned TestNet algod. Production never uses this. Default unset: never POST. 402security must GO before anyone sets it to `1`. Signer never reads BROADCAST. This flag never sends MainNet. |
| `LIVE402_PQ_FALCON_MAINNET_BROADCAST` | unset | Distinct MainNet flag. Default off. Automatic MainNet stays off. Unset is the kill switch: MainNet submit stops, routing continues. |
| `LIVE402_PQ_FALCON_MAINNET_CANARY` | unset | One-shot human canary gate. Default off. Worker, tick, and boot never read this. Live POST still needs this `=1` and `LIVE402_PQ_FALCON_MAINNET_BROADCAST=1`. |
| `LIVE402_PQ_FALCON_MAINNET_ADDRESS` | fly.toml (public MainNet address) | PRODUCTION public MainNet Falcon f1 address. Distinct from the archived TestNet address. Never a private key. |
| `LIVE402_PQ_SIGNER_TOKEN` | unset | TEST SUPPORT HMAC for the TestNet pq-anchor/1 client. Production never dials that signer. |
| `LIVE402_PQ_SIGNER_MAINNET_TOKEN` | unset | PRODUCTION request/response HMAC for `402signal-pq-signer-mainnet` (pq-anchor/3). Named, never valued in git. |
| `LIVE402_PQ_LOG` | unset | `0` forces transparency `unavailable` even if a signer is configured. |
| `LIVE402_HOT_REFRESH_S` | `600` (clamped 300–900) | Stale-claim threshold for the information-value refresh queue |
| `LIVE402_WARM_REFRESH_S` | `7200` (clamped 3600–10800) | WARM refresh interval (legacy due_warm helper) |
| `LIVE402_COLD_SWEEP_S` | `64800` (clamped 12–24h) | COLD rolling generation sweep cadence |
| `LIVE402_TRICKLE_SLEEP_S` | `2` (clamped 1–30) | Sleep between trickle pages |
| `LIVE402_CATALOG_REFRESH` | `1` | `0` disables the background trickle |
| `LIVE402_ROUTE_RPM` | `60` | paid `POST /route` per IP per minute. No User-Agent privilege. |
| `LIVE402_PREVIEW_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /preview` and MCP preview per IP per minute |
| `LIVE402_PUBLIC_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /pulse`, `GET /rails`, and `GET /attestation` per IP per minute (separate buckets) |
| `LIVE402_VALIDATE_RPM` | `60` | unpaid `POST /validate` / `GET /validate` and MCP `tools/call validate` per IP per minute |

## Fly (do not run until you have an account)

Single app **402signal**. Not HA. No second hostname.

```bash
fly launch --ha=false --name 402signal --no-deploy
fly secrets set CDP_API_KEY_ID=… CDP_API_KEY_SECRET=…
# After 402security GO only. An admin sets these; never paste values into chat. 402dev never holds them.
# fly secrets set LIVE402_PQ_LOG_SK_MAINNET=…
# fly secrets set LIVE402_PQ_SIGNER_MAINNET_TOKEN=…
fly deploy
fly ips list
```

`fly.toml` sets `app = "402signal"`, `internal_port = 8080`, `auto_stop_machines = "off"`, `min_machines_running = 1` on the **app** HTTP process (shared-cpu-1x 1GB). One app process. Do not deploy, `fly scale`, or set secrets from this PR. Production is MainNet-only. `LIVE402_PQ_FALCON_BROADCAST`, `LIVE402_PQ_SIGNER_TOKEN`, and `LIVE402_PQ_LOG_SK` stay unset on the public router. Automatic anchoring stays off.

## Namecheap BasicDNS (do not change until deploy)

Keep Namecheap nameservers. Use BasicDNS records only. Do not CNAME the apex. Delete parking / marketplace records first.

| Type | Host | Value |
|---|---|---|
| A | `@` | Fly shared IPv4 from `fly ips list` |
| AAAA | `@` | Fly IPv6 from `fly ips list` |
| CNAME | `www` | `402signal.fly.dev` |

## Bazaar

The 402 body includes `extensions.bazaar` with `info` + `schema` for `POST /route`, following [x402 bazaar](https://github.com/x402-foundation/x402/blob/main/specs/extensions/bazaar.md). Clients should echo it; we also attach it on settle so facilitators can index.

## Layout

```
live402/            package (server, route, probe, payment, facilitator, fixtures, shadow)
live402/shadow.py    on-disk catalog.sqlite (claims + FTS5). Not 402signal_observed.
live402/hydrate.py   finalist claimed-contract cache (bounded, TTL, gzip). Not 44k RAM schemas.
live402/policy.py    NL → structured constraints. Engine uses structured values only.
live402/reputation.py transparent components + documented V1 score + scoring-model hash.
live402/economics.py  rail economics with provenance. Same model for Base / Solana / Algorand.
live402/pq/         experimental C2SP log (RFC 9162 Merkle + tiles + receipts). PRODUCTION is MainNet-only.
live402/static/     GET / homepage (app.js, styles, dashboard.js)
live402/algod.py    pinned algod suggestedParams for the unpaid Algorand 402 extra
live402/data/       fixture catalog
tests/              unittest
Dockerfile          Python 3.12.14-slim (gh-150743), 0.0.0.0:$PORT (root; see docs/docker.md)
fly.toml            app 402signal, internal_port 8080, one machine
docs/backup.md      sqlite backup tooling + Fly human checklist (backups not claimed active)
docs/github-protection.md  branch protection (Protect main ruleset is active)
docs/automation-security-boundaries.md  bot and human roles. production command bans
docs/docker.md      non-root /data blocker (USER not added)
docs/merkle-bench.md  honest 10k/100k/1m frontier timings; SQLite commit is the bottleneck
```
