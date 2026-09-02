# SEC-ROUTER-003 / A-10 seller free-text fixtures

Offline catalog items whose **description**, **serviceName**, and schema
field **description** values carry prompt-injection strings
(`Ignore previous instructions`, system-prompt override).

These are tests-only. They are not loaded by `LIVE402_FIXTURE=1`
(`live402/data/fixtures.json` is unchanged). CI feeds them through
preview and MCP and asserts `catalog_claimed` / `untrusted` markings.

See `tests/test_sec_router_003_prompt_injection.py`.
SEC-TEST-002 XSS HTML escape fixtures stay in
`tests/fixtures/sec_test_002_xss/` and are not modified.
