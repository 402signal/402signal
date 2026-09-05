# Operator-owned direct-URL lab routing

This opt-in mode lets a real buyer test production `/route` against an operator-owned
seller without creating trusted route history or production PQ leaves. Configure
`LIVE402_LAB_ORIGINS` as a comma-separated list of exact HTTPS origins (no path or
trailing slash), for example `https://402signal-lab-ross.fly.dev`. Default: disabled.
An origin is an exclusion scope, not an SSRF exception. Public DNS/IP checks,
verification, price/network constraints, billable winner validation, settlement,
and durable economic replay retain their normal behavior.

Unpaid route challenges advertise `lab_testing.protocol=402signal-lab-route-v1`,
the configured origins, `history_promoted=false`, and `pq_recorded=false` in both
the body and encoded payment-required header. Lab clients must check this before
signing, and send `lab_test=402signal-lab-route-v1` with an explicit `url`.
A supplied marker for an unconfigured origin is rejected after verify and before
probe/settle. A marker cannot grant the exclusion to an ordinary seller.

For direct requests to a configured lab origin, classification is applied even
when the caller omits the marker. Direct lab probes do not persist a route batch
or attach trusted historical reputation; successful settlement does not promote
history or append a PQ route leaf. The payment replay ledger still records settled
and not-settled authorizations. Responses include:

```json
{"lab_testing":{"protocol":"402signal-lab-route-v1","traffic_class":"self_test","organic_demand":false,"history_promoted":false,"pq_recorded":false}}
```

Lab requests for `require_transparency` or `require_route_binding` are rejected
before settlement: this mode deliberately cannot satisfy production PQ evidence.
Normal unconfigured routes keep the existing history/PQ behavior.

Scope: explicit URL probe/selection, constraints, success-only billing and replay.
This is not a global catalog exclusion mechanism. Keep these lab endpoints out of
public catalogs and need-based campaigns. Search/discovery ranking, production PQ
append/anchor, and v4 binding need separate isolated integration tests. Never
represent operator-owned test purchases as external users or organic demand.

## Deployment

Review and deploy this router change through the normal process. Use the existing
production storage; do not reset or migrate replay/history/PQ data for this feature.
No signer configuration changes or signer deployment are required. A production
rollout still requires checking the exact deployed revision and ensuring no
unresolved anchor operation under the existing operations policy.

Stage the origin configuration for the reviewed router deployment; changing Fly
secrets without staging restarts the current app. Verify the resulting unpaid
challenge advertises the exact origin before enabling buyer routing. Disabling the
origin configuration makes the new client stop before signing. Never remove the
client capability check as a workaround for a missing deployment.

The initial seller-only lab purchases remain in the laptop ledger. The v0.3 buyer
uses that same ledger and accounts for previously consumed budget. Each routed run
reserves 3000 atomic routing USDC plus the maximum seller price. Normal typed misses
settle neither fee but conservatively retain the budget reservation. Payment and
seller delivery confirmation are independent; rechecks never resubmit or resume.
