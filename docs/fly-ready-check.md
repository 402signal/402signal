# Fly liveness and readiness checks

`/health` is the production liveness check. `/ready` is the independent
integrity endpoint (503 when the PQ log is smaller than `last_confirmed`,
or when storage/catalog/history/pq_log/replay-ledger checks fail).

Fly config keeps both checks. This preserves process liveness visibility
while preventing paid traffic from reaching a Machine whose durable
economic replay ledger or other required state is unavailable:

```toml
[[http_service.checks]]
  grace_period = "10s"
  interval = "15s"
  method = "GET"
  path = "/health"
  timeout = "5s"
```

```toml
[[http_service.checks]]
  grace_period = "10s"
  interval = "15s"
  method = "GET"
  path = "/ready"
  timeout = "5s"
```

Changing this file does not itself deploy. Deployment remains a separate
reviewed action.
