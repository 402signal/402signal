# SEC-TEST-002 seller/catalog XSS fixtures

Offline catalog items whose **description** and **schema** fields carry
classic HTML breakout strings (`<script>`, `javascript:`, `onerror=`).

These are tests-only. They are not loaded by `LIVE402_FIXTURE=1`
(`live402/data/fixtures.json` is unchanged). CI renders dashboard /
catalog / transparency HTML paths with these strings and asserts the
output is escaped, not a raw breakout. CSP is not relaxed.

See `tests/test_sec_test_002_xss.py`.
