# Key ceremony runbooks

Two ceremonies. Neither is executed in this PR. Never paste a mnemonic,
seed, PEM, or Fly secret into chat, a ticket, or git.

## Falcon-1024 (f1) MainNet account (Ross)

Use official `algokey` only.

1. On an air-gapped or equivalent machine: `algokey pq generate f1`
2. Write the mnemonic once to the intended offline store. One copy.
3. Derive and record the public Algorand address.
4. Confirm scheme `f1` (Falcon-1024). Reject f5 or any other scheme.
5. Set the public address later as `LIVE402_PQ_FALCON_MAINNET_ADDRESS`.
6. Load the secret only on `402signal-pq-signer-mainnet`. Never on the
   public 402signal router.
7. Do not reuse the TestNet Falcon secret or address.
8. Do not fund from this PR. See `docs/pq-funding.md`.

Empty `falcon.address` in `trust_root.v2.json` until this ceremony
finishes.

## Fresh Ed25519 log key (MainNet epoch)

Documented, not executed with real secrets in CI.

1. Generate a new Ed25519 seed offline. Do not reuse
   `LIVE402_PQ_LOG_SK` from TestNet.
2. Encode the public verifier key with the C2SP vkey format whose key
   name is `402signal.com/pq/log/mainnet-v1`.
3. Store the secret as Fly secret `LIVE402_PQ_LOG_SK_MAINNET` only.
   Code rejects silent fallback to `LIVE402_PQ_LOG_SK`
   (`reuse_testnet_sk=false`). Do not reuse the TestNet seed.
4. Publish the public half as `LIVE402_PQ_LOG_VKEY_MAINNET`.
5. Leave `trust_root.v2.json` `log_signature.vkey` empty until the
   public half is known. Do not commit the secret.

Generation is not run in CI. Tests may use ephemeral
`Ed25519PrivateKey.generate()` in process memory and must never print
the seed.

## Residual: in-process Ed25519 on the router

Today the TestNet log signer still loads `LIVE402_PQ_LOG_SK` in-process
on the public router (`live402/pq/receipt.py`). That is documented, not
casually isolated in this PR. Isolation of Ed25519 is a later change
unless a small, already-safe split appears. Falcon SK isolation is
already required and is not relaxed.

Boot never generates an Ed25519 key. Unset `LIVE402_PQ_LOG_SK` keeps
receipts `logged_uncheckpointed` or `unavailable`. `LIVE402_PQ_LOG=0`
is the log kill switch.
