# Proposed Fly readiness check (not applied in this PR)

`/health` stays the production liveness check. `/ready` stays the
integrity endpoint (503 when the PQ log is smaller than
`last_confirmed`, or when storage/catalog/history/pq_log checks fail).

This PR does not change `[[http_service.checks]]` behavior. The live
check remains:

```toml
[[http_service.checks]]
  grace_period = "10s"
  interval = "15s"
  method = "GET"
  path = "/health"
  timeout = "5s"
```

A later authorized deploy may *add* a second check. It must not replace
`/health` silently:

```toml
[[http_service.checks]]
  grace_period = "10s"
  interval = "15s"
  method = "GET"
  path = "/ready"
  timeout = "5s"
```

Do not `fly deploy` from this PR.
