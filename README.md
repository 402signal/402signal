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
- If `url` is set: must be `https`. GET (HEAD fallback), ~4s timeout. Response includes `live`, `status`, `latency_ms`, `has_402_challenge`, and a `health` snapshot.
- If only `need`: Coinbase / PayAI / GoPlausible discovery catalogs are fully paginated (`offset`/`limit`, not the first 20), fuzzy-match, probe up to 5, return the first live URL. If none live → **HTTP 503** `{ "live": false, "tried": n }` after settle (they paid for an honest miss).
- Dead upstream is **503** with the snapshot, never a fake live URL.
- `LIVE402_FIXTURE=1` uses `live402/data/fixtures.json`. No network.
- Paid **HTTP 200** includes `target: { method, inputSchema, outputSchema, accepts, facilitator, amountAtomic, displayAmount, timeoutSeconds }` so the next request body is invocable. If schema is missing, `live` may still be true with `invocable: false` and `miss_reason: "no_input_schema"`. `accepts[].extra.facilitator` is copied as `{url, feePayer, caip2, scheme}` — do not default to x402.org.
- `miss_reason` is a closed enum: `no_candidates`, `no_402_envelope`, `no_payto`, `reachable_200`, `probe_timeout`, `quote_expired`, `invalid_need`, `upstream_5xx`, `ssrf`, `no_input_schema`. HTTP 402 with no usable payTo is `no_payto` (honest miss, not retry-pay). Probe budget is under 60s; a hang returns **503** JSON immediately.
- `GET /health` is **HTTP 200** `{ "ok": true }` for Fly checks. Not a paid listing. Not a rails dump.
- `GET /preview?need=` is an unpaid cache preflight (`not_probed: true`, hits + prices + freshness + facilitator/method/inputSchema_present/rails_up, optional `also_on[]`). Optional `prefer_network=base|solana|algorand`. It does not probe and does not charge. Paid `POST /route` remains the fail-closed 402 probe.
- `GET /rails` lists the three pay-in networks, asset, amountAtomic, facilitators, feePayers, maxTimeoutSeconds, and per-rail up+latency. Cached. Not stuffed into `/health`.
- `GET /pulse` is a JSON snapshot of sample lookups (Base / Solana / Algorand). Allowlisted catalog hosts only. Query params are ignored — no caller-supplied URLs. Cached ~15s. Prices are dollars (USDC 6 decimals: `10000` → `$0.01`). Includes `samples` (up to 4 per chain).
- `GET /dashboard` is the same samples as HTML. Per-chain lookups you can try; click through to prefill the homepage form. Also free.
- GET `/` homepage is plain English: one line on what `/route` is, humans pointed at free `GET /preview`, agents at POST / MCP. Footer is 402signal.com / @402Signal. Hidden Pay $0.01 on Base (injected wallet only) signs one EIP-3009 authorization and POSTs PAYMENT-SIGNATURE. Algorand and Solana stay agent/CLI. A short “for agents” box shows `POST https://402signal.com/route` plus links to `/llms.txt`, `/preview`, `/rails`, `/openapi.json`, `/.well-known/x402.json`, and `/mcp.json`. Nav is Home / Pulse (GET `/pulse`); no `/dashboard` in homepage nav.
- `GET /route` is split by `Accept`: browsers (`text/html`) get the human page (HTTP 200). Agents (`application/json`) and curl with no Accept get HTTP 402 + bazaar + accepts (amount 10000). Agents that intend to pay should **POST**, not GET.
- Discovery: `GET /openapi.json`, `GET /mcp.json`, `GET /.well-known/x402`, `GET /.well-known/x402.json`, `GET /robots.txt`, `GET /llms.txt`, `GET /preview`, `GET /rails`. Paid `POST /route` is documented with `x-payment-info` and HTTP 402. MCP bazaar type is `mcp` + `toolName: route`.
- `POST /route` is rate-limited in memory (~60/min per IP, higher burst for Coinbase / PayAI / GoPlausible user-agents). `GET /preview` and unpaid MCP `tools/call preview` share a looser limiter (~180/min per IP, at least 2× the route cap). `GET /pulse` and `GET /rails` each have their own ~180/min per-IP limiter (same ballpark as preview, still looser than paid `/route`). `GET /health` stays unlimited `{ok:true}`. `429` when exceeded. Payment headers are redacted in logs. Responses send `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security: max-age=31536000` (no includeSubDomains; www is a CNAME and Fly has no extra hostnames), and `Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'`. script-src stays `'self'` (no CDN, no vendor wallet scripts). connect-src is `'self'` only; homepage Base pay POSTs `/route`. HEAD 200 on `/llms.txt` `/openapi.json` `/mcp.json` `/preview` `/rails` `/pulse`. Payment resource / OpenAPI `servers` / MCP resource are pinned to `https://402signal.com` (Host is not reflected). Probe DNS (`getaddrinfo`) times out in 2s.

Clients send v2 `PAYMENT-SIGNATURE` (base64 `PaymentPayload`) or v1 `X-PAYMENT`. Success/settle echo is `PAYMENT-RESPONSE`.

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
| `LIVE402_HISTORY_DB` | `/data/live402-history.sqlite` on Fly (`/tmp` fallback) | sqlite probe history (WAL, 0600, capped) |
| `LIVE402_ROUTE_RPM` | `60` | paid `POST /route` per IP per minute |
| `LIVE402_ROUTE_RPM_FACILITATOR` | `180` | higher burst for Coinbase / PayAI / GoPlausible UAs |
| `LIVE402_PREVIEW_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /preview` and MCP preview per IP per minute |
| `LIVE402_PUBLIC_RPM` | `180` (or 2× route, whichever is larger) | unpaid `GET /pulse` and `GET /rails` per IP per minute (separate buckets) |

## Fly (do not run until you have an account)

Single app **402signal**. Not HA. No second hostname.

```bash
fly launch --ha=false --name 402signal --no-deploy
fly secrets set CDP_API_KEY_ID=… CDP_API_KEY_SECRET=…
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
live402/            package (server, route, probe, payment, facilitator, fixtures)
live402/static/     GET / homepage (app.js, styles, dashboard.js)
live402/algod.py    pinned algod suggestedParams for the unpaid Algorand 402 extra
live402/data/       fixture catalog
tests/              unittest
Dockerfile          Python 3.12-slim, 0.0.0.0:$PORT
fly.toml            app 402signal, internal_port 8080, one machine
```
