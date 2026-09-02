# Route success vs log append (SEC-ROUTER-004 / A-14)

Paid `/route` settlement and transparency-log append are not one
atomic step.

## Paid 200 / 503

After a successful settle, HTTP 200 (live hit) and HTTP 503 (typed
miss) do **not** require a durable signed leaf.

Default (`require_transparency` unset or false):

- Append, sign, or checkpoint failure is best-effort.
- `pq_trust.transparency.status` may be `pending`,
  `logged_uncheckpointed`, or `unavailable`.
- `logged_uncheckpointed` means a durable leaf without a signed
  checkpoint. It is not `pending`.
- `unavailable` means append failed. It is not `pending`.

## require_transparency

When the request sets `require_transparency: true`, paid `/route`
fails closed unless a durable signed leaf exists (`status` `pending`
and state `checkpoint_signed`, with a receipt checkpoint).

`logged_uncheckpointed` is never treated as success on that path.
The response is HTTP 503 (`transparency receipt unavailable`).

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
