# Local route guard (preview)

A Node.js module with TypeScript declarations and **zero runtime dependencies**.
It verifies 402Signal's opt-in v4 route receipt, then compares the actual seller
request and fresh raw x402 challenge before the buyer's authorization callback.
It makes no network requests, stores no keys and does not create, sign, submit,
retry or execute payments.

This package is private and unpublished pending review. Use this repository's
source; do not assume a package with this name on a registry is this code.
The server contract must be deployed before real use. Existing v3 receipts fail
closed in this guard.

## Offline example

From the repository root with Node 22 or newer:

```sh
node --input-type=module <<'JS'
import { readFileSync } from 'node:fs';
import { verifyRoute } from './sdk/route-guard/index.mjs';
const vectors = JSON.parse(readFileSync('tests/fixtures/route-binding-v1.json'));
const sample = vectors.cases[0];
const terms = verifyRoute({
  routeResponseJson: JSON.stringify(sample.response),
  routeRequestJson: JSON.stringify(sample.request),
  trustedLogVkey: vectors.trusted_vkey, // PUBLIC TEST KEY, never production.
  request: { url: sample.response.url, method: sample.method, body: Buffer.from(sample.body) },
  challenge: { status: 402, bodyText: JSON.stringify(sample.challenge) },
  now: sample.now, // Fixtures only. Omit this in real use.
});
console.log(terms.model, terms.request.method, terms.accepted.network);
JS
```

## Integration boundary

1. Keep the actual `/route` request JSON containing
   `require_route_binding: true`. Keep its response as **raw JSON text** until
   this guard parses it. Pre-parsing and serializing untrusted JSON loses
   duplicate-key evidence.
2. Pin the log verification key through your trusted configuration,
   independently of the route response. Never adopt a key offered by that same
   response. A log key rotation requires an explicit pin update.
3. With your HTTP client and SSRF policy, obtain the seller's unpaid 402
   challenge using the **same URL, method and body bytes** the route observed.
   Disable automatic redirects. Pass the raw body text, numeric HTTP status and
   any `PAYMENT-REQUIRED` / `X-PAYMENT-REQUIRED` header values to the guard.
   Malformed or disagreeing channels are rejected.
4. Call `withVerifiedRoute(options, callerAuthorize)` immediately before your
   payment flow. The callback receives detached, deeply frozen `accepted` terms
   from the authenticated envelope. Use these terms, not response display fields
   or an agent's rewritten parameters. Your official rail validator and wallet
   still check actual transaction effects and enforce the buyer's budget,
   replay/idempotency policy, recipient, token, chain and signature scope.

```js
const result = await withVerifiedRoute(options, async verified => {
  // buyerAuthorizeAndExecute is YOUR independently secured wallet flow.
  // Validate the actual transaction against verified.accepted and buyer policy.
  return buyerAuthorizeAndExecute(verified);
});
```

The callback is invoked once after verification; a thrown error or rejected
promise is never retried. This does **not** provide economic exactly-once
semantics. Repeated calls can invoke it again: the buyer must reserve its own
durable payment fingerprint. A rejected guard must not trigger automatic
routing-fee or seller-payment retries.

For human approval, wait outside the callback, obtain fresh evidence and repeat
verification immediately before signing. There is no approval queue, delegated
signer, or permission to ignore expiry. No AC2, AP2, UCP or card-network integration.

## What passes

The salted commitment, leaf hash, Merkle inclusion, Ed25519 checkpoint signature
and pinned origin must verify. The signed original route request must match the
caller request; the response binding must match its signed counterpart. The
entire current x402 v2 envelope, selected accept, exact URL/method/body hash and
observation-based expiry must match.

This profile covers the existing Base, Solana and Algorand `exact` rails. Unknown
extensions, lossy/malformed JSON, redirects and different bodies fail closed.
The server probes GET or justified POST `{}` only. Arbitrary POST inputs and
rotating/personalized quotes are outside the profile. Opaque `extra` is hashed,
not interpreted as proof an arbitrary transaction is safe. Default TTL is 60
seconds, maximum 120; it is not a promise the seller will honor a quote that long.

The immediate checkpoint is Ed25519. A later cumulative Algorand Falcon anchor
does not turn a pending receipt into confirmed evidence and is not verified by
this guard. This proves matched observed terms, not delivery, identity, output
quality or safety of a compromised buyer runtime.

Routing remains **$0.003 USDC only when a valid live route is found**. Normal typed
misses are not settled. Seller payment is separate. A settled routing request
whose required receipt later fails is still billed: inspect `billing`, preserve
that outcome and do not retry payment to repair it.

## Tests

```sh
npm --prefix sdk/route-guard test
npm --prefix sdk/route-guard run check
```

Shared Python-produced signed fixtures cover all rails, odd and power-of-two
trees, non-last-leaf proofs, POST body binding and Unicode ordering. Tests reject
quote/policy/proof mutations, stale receipts, parser ambiguity, wrong keys and
resource changes before callback. No live wallet, payment or signer is needed.

See [the v1 contract](../../docs/proof-carrying-route-v1.md) for format, privacy,
rollout and rollback details.
