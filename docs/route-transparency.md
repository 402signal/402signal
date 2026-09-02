# Route success vs log append (SEC-ROUTER-004 / A-14)

Paid `POST /route` HTTP 200 and HTTP 503 are probe and settle outcomes.
They do not require a durable signed leaf unless the caller set
`require_transparency`.

## Non-atomicity

Settlement and log append are not one transaction. A crash after settle
and before append can leave a paid 200 or 503 with no leaf.
`pq_trust.transparency.status` then reports `unavailable` or
`logged_uncheckpointed`. That is not a signed receipt.

`require_transparency: true` fails closed: HTTP 503
`transparency receipt unavailable`. `logged_uncheckpointed` is never a
success under that gate.

Crash-before-append still produces no leaf and no checkpoint.

## Verifier key

`public_vkey()` prefers the epoch env vkey over sqlite `meta.vkey`.
Env wins. A stale persisted vkey is not advertised or used to bind a
checkpoint when the env vkey is set.
