# pq-anchor/2 router wire corpus (offline)

This directory is the **public-router serialization gate** for the
MainNet `pq-anchor/2` request/reject wire.

It is **not** a live signer integration. Full Go parser and HMAC-verify
tests run on **private signer CI under Ross** (`402signal-pq-signer`,
Ross-only). This public repo must not receive credentials to that
signer. `CROSS_REPO_EXTRA_SECRET_REQUIRED=NO`.

Ross can copy these pinned bytes into signer CI and feed them to the
Go decoder. No MainNet network contact. No production HMAC token. No
Falcon private key.

## Why this exists

PR #45 showed Python and Go unit tests can pass while JSON
`size_version` as a number (`1`) vs a string (`"1"`) still breaks the
live wire. Go `policy.Snapshot.SizeVersion` is a string. The router
must emit `"size_version":"1"` and refuse int `1` at `narrow_policy`.

## Files

| File | Meaning |
|---|---|
| `request.json` | Exact `encode_request_line` bytes for a well-formed request (compact JSON, no trailing newline) |
| `reject.json` | Exact MainNet HMAC-reject reply: `{"ok":false,"error":"hmac"}` |
| `hmac_canonical.txt` | 380-byte HMAC preimage golden (`size_version=1\n`, `v=pq-anchor/2`) |
| `manifest.json` | Corpus metadata (protocol, no extra secret, offline) |

`request.json` is built by the router test from real
`narrow_policy` / `build_request` / `encode_request_line`. HMAC uses
the fixture-only token `fixture-hmac-token-not-a-secret` (not a
production secret). On MainNet the signer would reject this MAC with
`reject.json` and must not return `signed` or `pqsig`.

## Router CI vs signer CI

- Router CI (`tests/test_pq_cross_repo_wire.py`): rebuilds request
  bytes, asserts string `size_version` on the JSON wire, asserts the
  380-byte HMAC preimage, asserts int `size_version` is rejected, and
  pins the hmac-reject reply shape.
- Signer CI (Ross): parse `request.json` with the Go decoder, confirm
  `SizeVersion` unmarshals as string, HMAC-verify with the signer
  token (not present here), and confirm the invalid-MAC path returns
  exactly `reject.json` with no authorization material.

Do not add a GitHub secret on this public router for private-signer
access. Do not clone or modify `402signal-pq-signer` from this PR.
