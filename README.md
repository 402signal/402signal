# 402Signal

Fail-closed live-endpoint x402 router. We probe. You pay $0.01 for a live URL or an honest miss.

- **Live site:** https://402signal.com
- **Paid API:** `POST /route` — $0.01 USDC
- **MCP:** https://402signal.com/mcp.json
- **OpenAPI:** https://402signal.com/openapi.json

Dated 2026-08-29. Production verify + settle. No private payment keys. We never pay upstream.

## Run locally

Python 3.11+ (stdlib plus optional `cryptography` for Coinbase CDP JWTs). Local default is **127.0.0.1:8081**. Fly / Docker bind **0.0.0.0:$PORT** (default 8080).

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
```

- No valid payment and `LOCAL_FREE` unset → **HTTP 402**. One 402 lists three accepts (Base, Solana, Algorand) plus the bazaar extension. We do not probe.
- Valid payment: verify with the matching facilitator, then probe, then settle. An unverified header never opens the gate.
- If `url` is set: must be `https`. Unpaid probe is GET first, then POST `{}` only if GET was not a live 402 and POST is justified (GET 405/501, or GET is clearly not an x402 challenge). Never POST seller-declared or catalog-declared input bodies. If the catalog says a body is required and GET+POST `{}` cannot establish a live 402, the typed miss is `unsafe_to_probe`. DNS is resolved once (`getaddrinfo`, 2s); the TCP/TLS connection is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host set to the original hostname (re-checked and re-pinned on each redirect hop). Fail closed if pin/SNI/Host cannot be applied. ~4s timeout. Response includes `live`, `status`, `latency_ms`, `has_402_challenge`, `selected_payment`, and a `health` snapshot. 402Signal settles the $0.01 routing payment; it does not pay the selected merchant.
- If only `need`: federated need-scoped search (local FTS on `catalog.sqlite` + Coinbase / PayAI / GoPlausible) at request time (CDP `/discovery/search`; PayAI / GoPlausible search or a small first-pages fetch), union/dedupe/rank that working set, hydrate only the top ~5–10 finalists with claimed method/schema/toolName (bounded, TTL, optional gzip on disk — never a 44k RAM schema index), then probe adaptively (first 3, expand 2–4 if no winner; hard ceiling 20). Return the best currently observed live option (not the first live URL, not a catalog-only rail). Live hits are write-through upserted onto disk. Catalog claims stay on `claimed.payment_options` and `catalog.sqlite` `accept_claims`; `target.accepts` and selection use the current HTTP 402 envelope only. Claimed schemas are not observed payment options. If none live → **HTTP 503** `{ "live": false, "tried": n }` after settle (they paid for an honest miss). Optional structured constraints: `max_price_usd`, `max_total_cost_usd` (merchant + known fees; unknown fee fails closed), `max_probe_latency_ms`, `max_service_latency_ms` (historical p50, not probe RTT), `max_settlement_latency_ms` (settlement/finality, not probe RTT), `require_invocable`, `networks`, `min_observations`, `min_observed_success`, `min_reputation_score`, `min_reputation_confidence`. `max_latency_ms` is a probe-RTT alias. Unknown measured values fail closed. Optional `objective`: `best` / `cheapest` / `fastest` / `most_reliable` / `lowest_total_cost` / `fastest_settlement`. Optional `policy` / need phrasing such as `weather under $0.01 and 300ms` compiles to structured constraints; "established usage" / "strong observed evidence" compile to `min_observations=10`; vague "high reputation" stays unresolved; settlement / total-cost language compiles only with a numeric bound. The engine uses structured values only. Paid `/route` and unpaid `/preview` include transparent `reputation` components (observed / usage / tenure / stability / source_count) plus V1 `reputation_score`, `reputation_confidence`, and `scoring_model_id` / `scoring_model_hash`. Rail `economics` (merchant price, fees, settlement/finality) sit on the selected payment option and on `compared[]`, each field labeled `402signal_observed`, `protocol_reference`, or `unknown`. Same model on Base, Solana, and Algorand — no algo bonus. Catalog is not a 0–100 badge. Pulse stays facts; rates stay hidden below `n=10`. Unique payer addresses are never listed. Most usage/settlement/unique-payer fields are unknown on first ship because 402Signal has probe history, not a settlement ledger.
- Dead upstream is **503** with the snapshot, never a fake live URL.
- `LIVE402_FIXTURE=1` uses `live402/data/fixtures.json`. No network.
- Paid **HTTP 200** includes `target: { method, inputSchema, outputSchema, accepts, facilitator, amountAtomic, displayAmount, timeoutSeconds }` (envelope accepts only) and `selected_payment: { rail, network, asset, amount_atomic, display_amount, normalized_usd, payTo, facilitator }` for the exact observed option that won. `payable` requires a complete observed option; `invocable` is payable plus input schema. If schema is missing, `live` may still be true with `invocable: false` and `miss_reason: "no_input_schema"`. `accepts[].extra.facilitator` is copied as `{url, feePayer, caip2, scheme}` — do not default to x402.org.
- `miss_reason` is a closed enum: `no_candidates`, `no_402_envelope`, `no_payto`, `reachable_200`, `probe_timeout`, `quote_expired`, `invalid_need`, `upstream_5xx`, `ssrf`, `no_input_schema`, `constraints_unmet`, `probe_budget_exhausted`, `probe_limit_reached`, `unsafe_to_probe`. HTTP 402 with no usable payTo is `no_payto` (typed miss, not retry-pay). Probe budget is under 60s; a hang returns **503** JSON immediately. `stop_reason` and `probe_ceiling` say why probing stopped.
- `GET /health` is **HTTP 200** `{ "ok": true }` for Fly checks. Not a paid listing. Not a rails dump.
- `GET /preview?need=` is an unpaid request-time catalog search (`not_probed: true`, hits + prices + freshness + facilitator/method/inputSchema_present/rails_up, optional `also_on[]`). Optional `prefer_network=base|solana|algorand` ranks that rail first but still searches all three. Optional `networks=solana` (repeat or comma-separate) restricts which rails are queried. `discovery_via` is a compact per-rail how-found map; `discovery_exhaustive` is true only when the returned set is known complete. It does not probe and does not charge. Paid `POST /route` remains the fail-closed 402 probe.
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
- `GET /pq/log/checkpoint` and `GET /pq/log/tile/*` are an **experimental** C2SP transparency log (tlog-checkpoint@v1.0.0 + tlog-tiles@v0.1.0). Origin `402signal.com/pq/log`. This is product-GO for the log in CI/local. It is **not** MainNet-anchored and not a live Falcon inclusion. Paid `POST /route` may include optional `pq_trust.transparency` `{status: pending|unavailable, log_origin, index, checkpoint_size, receipt}`. `pending` means a durable leaf plus a signed checkpoint. `unavailable` means the log was down (not pending). `payment_authorization.pq_native` is always false. No `/trust` page. No homepage PQ copy. Production can turn the log ON by setting Fly secret `LIVE402_PQ_LOG_SK` (never baked into git; never auto-generated on boot). 402security must GO before that Fly secret is set and before any Falcon spend.
- `POST /route` is rate-limited in memory (~60/min per IP, higher burst for Coinbase / PayAI / GoPlausible user-agents). `GET /preview` and unpaid MCP `tools/call preview` share a looser limiter (~180/min per IP, at least 2× the route cap). `GET /pulse` and `GET /rails` each have their own ~180/min per-IP limiter (same ballpark as preview, still looser than paid `/route`). `GET /health` stays unlimited `{ok:true}`. `429` when exceeded. Payment headers are redacted in logs. Responses send `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security: max-age=31536000` (no includeSubDomains; www is a CNAME and Fly has no extra hostnames), and `Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'`. script-src stays `'self'` (no CDN, no vendor wallet scripts). connect-src is `'self'` only; homepage Base pay POSTs `/route`. HEAD 200 on `/llms.txt` `/openapi.json` `/mcp.json` `/preview` `/rails` `/pulse`. Payment resource / OpenAPI `servers` / MCP resource are pinned to `https://402signal.com` (Host is not reflected). Probe DNS (`getaddrinfo`) times out in 2s; the TCP/TLS connection is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host set to the original hostname (re-pinned on redirects). Seller-declared catalog bodies are never POSTed; unjustified POST `{}` is skipped; required-body misses are `unsafe_to_probe`.

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
| `LIVE402_PQ_LOG_DB` | `/data/pq-log.sqlite` on Fly (`/tmp` fallback) | Experimental C2SP log. Separate file from catalog and history. **Not HTTP-exposed** as a sqlite dump; read API is `/pq/log/*` only. |
| `LIVE402_PQ_LOG_VKEY` | unset | Ed25519 log verifier key (public). Set on boot from the public half of `LIVE402_PQ_LOG_SK` when that secret loads. Never a private key. |
| `LIVE402_PQ_LOG_SK` | unset | Optional Ed25519 log signing seed: raw 32-byte seed as hex, or PKCS8 PEM. Fly secret only — never commit, never paste into chat. If unset (or `LIVE402_PQ_LOG=0`), receipts stay `transparency.status=unavailable`. Malformed value fails closed (no random key; `/route` still serves). Boot never generates a key. Ross/402QA sets `fly secrets set LIVE402_PQ_LOG_SK=…`; 402dev never holds it. 402security must GO before this secret is set. |
| `LIVE402_PQ_FALCON_ADDRESS` | unset | Algorand address for constructed (not broadcast) Falcon anchors. |
| `LIVE402_PQ_LOG` | unset | `0` forces transparency `unavailable` even if a signer is configured. |
| `LIVE402_HOT_REFRESH_S` | `600` (clamped 300–900) | Stale-claim threshold for the information-value refresh queue |
| `LIVE402_WARM_REFRESH_S` | `7200` (clamped 3600–10800) | WARM refresh interval (legacy due_warm helper) |
| `LIVE402_COLD_SWEEP_S` | `64800` (clamped 12–24h) | COLD rolling generation sweep cadence |
| `LIVE402_TRICKLE_SLEEP_S` | `2` (clamped 1–30) | Sleep between trickle pages |
| `LIVE402_CATALOG_REFRESH` | `1` | `0` disables the background trickle |
| `LIVE402_ROUTE_RPM` | `60` | paid `POST /route` per IP per minute |
| `LIVE402_ROUTE_RPM_FACILITATOR` | `180` | higher burst for Coinbase / PayAI / GoPlausible UAs |
| `LIVE402_PREVIEW_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /preview` and MCP preview per IP per minute |
| `LIVE402_PUBLIC_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /pulse`, `GET /rails`, and `GET /attestation` per IP per minute (separate buckets) |
| `LIVE402_VALIDATE_RPM` | `60` | unpaid `POST /validate` / `GET /validate` and MCP `tools/call validate` per IP per minute |

## Fly (do not run until you have an account)

Single app **402signal**. Not HA. No second hostname.

```bash
fly launch --ha=false --name 402signal --no-deploy
fly secrets set CDP_API_KEY_ID=… CDP_API_KEY_SECRET=…
# After 402security GO only. Ross/402QA sets this; never paste the value into chat. 402dev never holds it.
# fly secrets set LIVE402_PQ_LOG_SK=…
fly deploy
fly ips list
```

`fly.toml` already sets `app = "402signal"`, `internal_port = 8080`, `auto_stop_machines = "off"`, `min_machines_running = 1`, one VM.

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
live402/pq/         experimental C2SP log (RFC 9162 Merkle + tiles + receipts). Not MainNet.
live402/static/     GET / homepage (app.js, styles, dashboard.js)
live402/algod.py    pinned algod suggestedParams for the unpaid Algorand 402 extra
live402/data/       fixture catalog
tests/              unittest
Dockerfile          Python 3.12-slim, 0.0.0.0:$PORT
fly.toml            app 402signal, internal_port 8080, one machine
```
