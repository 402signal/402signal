# Settlement provenance isolation

`settled_route_observation=0` is not enough. Tentative route observations
must not move trusted product state.

## Trust classes

| Class | How it is written | Trusted? |
|---|---|---|
| `INDEPENDENT` | `record_probe` default (validate/health/manual) | yes |
| `SCHEDULED` | `record_probe` with `scheduled=true` or `trust_class=SCHEDULED` | yes |
| `ROUTE_TENTATIVE` | `persist_route_batch` before settlement | no (diagnostics only) |
| `ROUTE_SETTLED` | `mark_batch_settled` after a successful pay | yes (may promote) |

Trusted classes may update `url_state`, `last_checked`, `last_success_402`,
payTo pending/change, price/schema change clocks, `summary`, `rank_hints`,
`preview` / history joins, reputation evidence, pulse rates, and shadow
`last_verified` / `last_routed`.

`ROUTE_TENTATIVE` rows are stored for diagnostics and attestation of that
route batch. They do not write `url_state` and do not touch shadow
freshness.

## Old rows

Additive column `trust_class` on `probes` and `observations`.

- old `settled_route_observation=0` -> `ROUTE_TENTATIVE`
- old `settled_route_observation=1` -> `INDEPENDENT` (trusted)

We do not rewrite historical `url_state` from before this change.

## Pre-settlement payTo risk

Read-only: compare the tentative observation to trusted `url_state`.
The current `/route` result may flag `payTo_pending` / `payTo_changed`
so selection and `accept_payTo_change` still work. Failed settlement
does not persist those flags.

## PayTo rotation (trusted observations only)

1. Trusted A is `last_payTo`.
2. Two failed tentative B observations after A do not establish B and
   do not write `pending_payTo`.
3. Settled B after trusted A sets `pending_payTo=B` (`last_payTo` stays A).
4. A later independent trusted B, or a later settled B, may establish B.
5. `accept_payTo_change` still opts into selecting a first-change dest
   on the current request.

## mark_batch_settled

One sqlite transaction:

1. Mark matching tentative probes/observations `ROUTE_SETTLED`.
2. Recompute each URL's trusted `url_state`.
3. Commit, then update shadow freshness for URLs that were actually
   applied.

Late settlement of an older observation does not overwrite a newer
trusted `last_checked` / payTo / price / schema row.
