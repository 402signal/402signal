# Route success vs log append (SEC-ROUTER-004 / A-14)

Paid `/route` settlement and transparency-log append are not one
atomic step.

## Settled winner / free miss

Only a valid live eligible HTTP 200 winner is settled and passed to the
transparency append path. A normal typed HTTP 503 miss is not settled,
does not append a route-decision leaf, and cannot cause a Falcon anchor
solely for that request.

Default (`require_transparency` and `require_route_binding` unset or false):

- After a successful settlement, append, sign, or checkpoint failure is best-effort.
- `pq_trust.transparency.status` may be `pending`,
  `logged_uncheckpointed`, or `unavailable`.
- `logged_uncheckpointed` means a durable leaf without a signed
  checkpoint. It is not `pending`.
- `unavailable` means a signed receipt could not be produced. An append may
  already have occurred. It is neither `pending` nor proof that no leaf exists.

## require_transparency

When the request sets `require_transparency: true` or `require_route_binding: true`,
a settled winner fails closed unless a durable leaf and signed checkpoint
receipt can be returned (`status` `pending` and state `checkpoint_signed`).
The latter flag selects [v4](proof-carrying-route-v1.md) and implies required
transparency even if `require_transparency` is explicitly false.

The immediate checkpoint is Ed25519-signed. `pending` does not mean an
Algorand Falcon anchor is confirmed; confirmation is a separate lifecycle.

`logged_uncheckpointed` is never treated as success on that path.
The response is HTTP 503 (`transparency receipt unavailable`) but its
`billing` object remains explicit: settlement was attempted and succeeded.
The request is not described as free, and no second settlement is attempted.
Clients must inspect `billing.settlement_state` on every HTTP 503 before
retrying. This settled transparency failure reports `settled`; a normal miss
reports `not_attempted`; a lost or malformed settlement reply reports
`unknown` with `settled:null`, and that authorization must not be reused.

For an opted-in v4 request, an unprovable binding is rejected before settlement
as a free typed miss, without a route-decision leaf. Once settlement succeeds,
a receipt failure must not trigger a second settlement or a second v4 append:
the first append may already be durable. Client-side expiry or a changed seller
challenge also cannot undo the original routing-fee settlement.

Crash-before-append still leaves tree size 0 and no receipt. Keep
`test_crash_after_queue_before_append_no_receipt` and
`test_crash_after_durable_before_sign_no_dangling_promise` in
`tests/test_pq_receipt.py`.

## Public vkey (fail closed / env wins)

`public_vkey()` advertises the env/trust vkey when it is set. A stale
sqlite `meta.vkey` must not win. Sqlite is used only when env is
empty.

## Scope

Documentation and fail-closed choice of the advertised vkey. This
does not make settle and append a single transaction.
