# Prepare Falcon transparency anchoring for Algorand MainNet

This PR prepares MainNet. It does not cut over.

- NO MAINNET TRANSACTION WAS SUBMITTED
- MAINNET BROADCAST DEFAULT OFF
- AUTOMATIC ANCHORING OFF

Production live path stays TestNet (`LIVE402_PQ_FALCON_NETWORK=testnet`,
`LIVE402_PQ_LOG_DB=/data/pq-log.sqlite`). Website copy stays TestNet.
Paid `/route` never waits for chain. Public status is CONFIRMED only.

## Threat-model delta

| Before | After this PR |
|---|---|
| MainNet genesis was rejected as a leftover path | Exact genesis match for the allowed network. MainNet submit is a separate, fail-closed gate. |
| One broadcast env | TestNet `LIVE402_PQ_FALCON_BROADCAST` cannot send MainNet. MainNet requires `LIVE402_PQ_FALCON_MAINNET_BROADCAST=1` and every other gate. |
| One log identity | TestNet shard archived in place. Fresh MainNet identity (origin, epoch, vkey, sqlite) prepared and empty. |
| Fee treated minFee/suggested as flat | Official Falcon fee: max(fee_per_byte * signed size, 3*base min). Uncongested floor 3000. Cap 30000. |
| Confirm was TestNet-shaped | Submit and confirm hosts have separate hardcoded allowlists. Confirm is fetch+decode+verify. AlgoNode and Nodely are the same org. |
| Signer was TestNet-only | Spec for `402signal-pq-signer-mainnet`. Signer still never broadcasts. |

Residual: Ed25519 log signing may still be in-process on the router.
Falcon SK must never be. Unexpected non-PQ1 activity on the Falcon
account is an incident.

## Gating (MainNet submit requires ALL)

1. `LIVE402_PQ_FALCON_NETWORK=mainnet`
2. Exact MainNet genesis ID `mainnet-v1.0` and hash
   `wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=`
3. Configured `LIVE402_PQ_FALCON_MAINNET_ADDRESS`
4. Explicit `LIVE402_PQ_SIGNER_MAINNET_TOKEN`
5. Valid checkpoint (origin/tree/root/signed-note)
6. Semantic `validate_signed_txn(..., expected_network="mainnet")` OK
7. Fee within `MAX_FEE=30000`
8. Allowlisted MainNet submit host (`mainnet-api.algonode.cloud`)
9. `LIVE402_PQ_FALCON_MAINNET_BROADCAST=1`
10. Human one-shot also requires `LIVE402_PQ_FALCON_MAINNET_CANARY=1`
11. Not fixture/CI for a live POST (tests may inject `send_fn`)

Any mismatch fails closed. Worker `maybe_submit` / `tick` never MainNet
send even if the env gates are later set.

`submit_mainnet_canary` stays unwired (not called from worker, tick, or
boot). Tests exercise authorize, validate, and a mocked would-POST hook.
A live MainNet POST still needs both flags, a human `authorize_human_canary`,
and every other gate. Automatic stays off.

Kill switch: unset `LIVE402_PQ_FALCON_MAINNET_BROADCAST`. Routing and the
log still work. Do not destroy the Falcon key.

## Env vars (new or newly distinct)

| Variable | Default | Meaning |
|---|---|---|
| `LIVE402_PQ_FALCON_MAINNET_BROADCAST` | unset | `1` is required for a human MainNet canary. Default off. Kill switch when unset. |
| `LIVE402_PQ_FALCON_MAINNET_CANARY` | unset | One-shot gate. Default off. Worker never reads it. |
| `LIVE402_PQ_FALCON_MAINNET_ADDRESS` | unset | Public MainNet Falcon f1 address after Ross ceremony. |
| `LIVE402_PQ_SIGNER_MAINNET_TOKEN` | unset | HMAC token name only. Never a committed value. |
| `LIVE402_PQ_SIGNER_MAINNET_HOST` | `402signal-pq-signer-mainnet.internal` | 6PN signer host |
| `LIVE402_PQ_SIGNER_MAINNET_PORT` | `9091` | Never 8080 |
| `LIVE402_PQ_LOG_EPOCH` | unset (`testnet-v1`) | Exact `testnet-v1` or `mainnet-v1`. Typos error. `NETWORK=mainnet` requires `mainnet-v1` plus MainNet DB, origin, trust v2, Falcon address, and signer. |
| `LIVE402_PQ_LOG_ORIGIN` | TestNet origin | Override. MainNet default is `402signal.com/pq/log/mainnet-v1` |
| `LIVE402_PQ_LOG_SK_MAINNET` | unset | Fresh MainNet Ed25519 log SK only. Code rejects fallback to `LIVE402_PQ_LOG_SK` (`reuse_testnet_sk=false`). Never commit. |
| `LIVE402_PQ_LOG_VKEY_MAINNET` | unset | Public Ed25519 vkey for the MainNet epoch |

Unchanged live path: `LIVE402_PQ_LOG_DB=/data/pq-log.sqlite`,
`LIVE402_PQ_FALCON_NETWORK=testnet`, TestNet broadcast unset.

## Fresh log and trust v2

- TestNet file `/data/pq-log.sqlite` stays. Archive it read-only
  (`docs/pq-testnet-archive.md`).
- MainNet file `/data/pq-log-mainnet.sqlite` starts empty.
- Origin `402signal.com/pq/log/mainnet-v1`. Public HTTP prefix stays
  `/pq/log`.
- `live402/pq/trust_root.v2.json` is prepared (no secrets, Falcon
  address empty, broadcast policy off, `reuse_testnet_sk=false`).
  `GET /pq/log/trust` still serves the live TestNet v1 descriptor.
- Code enforces `reuse_testnet_sk=false`: MainNet epoch and
  `NETWORK=mainnet` load `LIVE402_PQ_LOG_SK_MAINNET` only. Silent
  fallback to `LIVE402_PQ_LOG_SK` (or the same public key in both
  envs) raises `SignerConfigError`.

## Fees, providers, recovery

- Fee: max(fee_per_byte * Falcon signed size, 3 * protocol base min).
  Uncongested floor 3000, not 1000. Cap 30000. Caller cannot select
  the fee. 402security: please review this calculation.
- Submit and confirm hosts are separate hardcoded allowlists.
  Default MainNet submit is `mainnet-api.algonode.cloud`. Default
  confirm is `mainnet-idx.algonode.cloud`. A second confirm host
  `mainnet-idx.4160.nodely.dev` is allowlisted. AlgoNode and Nodely
  are the same organization (legacy brand plus current hostname).
  `confirmation_policy.independent_provider` stays false on the
  committed default (both hosts Nodely). Computed policy is true only
  when `LIVE402_PQ_CONFIRM_PROVIDER=tatum` (primary) or `nownodes`
  (failover). AlgoNode, Nodely, Allo, and Oanor are the same trust
  domain. Org labels are from public company records, not a legal
  audit. AlgoExplorer public indexer hosts return HTML landing pages,
  not JSON. Production MainNet GO requires the Tatum/NowNodes
  allowlist plus the API key as a Fly secret later (not in this PR).
  Fetch+decode+semantic verify is still required. MainNet never
  treats submit-host pending as confirmation.
- Recovery drills A-F run as fixture tests (`tests/test_pq_recovery_drills.py`).
  Backup identity drill: `scripts/pq_log_restore_drill.py`.
- Monitoring: `live402/pq/monitor.py` `snapshot()` is the operator
  structure (epoch, network, tree, last authorized/submitted/confirmed,
  gaps, ages, signer, providers, fee/cap, last error, DB errors,
  recovery conflicts). GET `/ready` uses a boolean subset only.
  Unexpected non-PQ1 Falcon account activity is an incident.

## Runtime (PRECHECK B / P1)

Docker runtime is `python:3.12.14-slim` pinned to official index digest
`sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc`.
3.12.14 includes gh-150743 (outbound `http.client` interim-1xx and
chunked-trailer limits). 3.12.11 does not. Inbound `http_body` bounds
do not close this P1. See `docs/docker.md`.

## Proof MainNet is disabled

- `automatic_mainnet_enabled()` returns False
- `send_if_allowed` returns None when NETWORK=mainnet
- `submit_mainnet_canary` is not called from worker/tick/boot
- Both MainNet flags default unset. Unset broadcast is the kill switch
- `LIVE402_FIXTURE=1` never dials live algod
- fly.toml does not enable either MainNet flag
- CI: `LIVE402_FIXTURE=1 python -m unittest discover -s tests`

See `docs/signer-mainnet-spec.md`, `docs/pq-key-ceremony.md`,
`docs/pq-funding.md`, and `docs/pq-first-production-event.md`.
