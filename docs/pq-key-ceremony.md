# Key ceremony runbooks

Two ceremonies. Neither is executed in this PR. Never paste a mnemonic,
seed, PEM, or Fly secret into chat, GitHub, Cursor, Slack, email,
screenshots, Fly secrets, shell history, or cloud notes.

## Falcon-1024 (f1) MainNet account (Ross)

Use official `algokey` only. New MainNet Falcon account. Do not reuse
the TestNet Falcon secret or address.

```
algokey pq generate --scheme f1 --keyfile <secure-path>
algokey pq info --keyfile <secure-path>
```

1. On an air-gapped or equivalent machine, generate with the commands
   above. Confirm scheme `f1` (Falcon-1024). Reject f5 or any other
   scheme.
2. Write the mnemonic once when `algokey` prints it. The mnemonic
   cannot be extracted from the keyfile later.
3. Make at least TWO separate offline mnemonic backups. Paper or
   equivalent offline stores. Not the same drawer, disk, or cloud.
4. Record the public Algorand address from `algokey pq info`.
5. Offline recovery drill before MainNet GO (no production reuse of the
   temp file):
   - generate
   - record the public address
   - two offline mnemonic backups
   - isolated `algokey pq import` to a TEMP keyfile
   - `algokey pq info --keyfile <temp>`
   - match the recorded address
   - destroy the temp keyfile
   - production keyfile lives only on `402signal-pq-signer-mainnet`
6. Set the public address later as `LIVE402_PQ_FALCON_MAINNET_ADDRESS`.
   Never on the public 402signal router as a secret.
7. Do not fund from this PR. See `docs/pq-funding.md`.

Empty `falcon.address` in `trust_root.v2.json` until this ceremony
finishes.

## Fresh Ed25519 log key (MainNet epoch)

Documented, not executed with real secrets in CI. Fresh MainNet
Ed25519. Do not reuse the TestNet Ed25519 secret or public key.
Ross-only production steps (stdin/files/umask, no CLI secret args,
retire the compromised MainNet SK, empty the 2-leaf test DB) live in
`docs/runbooks/mainnet-prelaunch-reset.md`. This page stays the
ceremony outline.

1. Generate a new Ed25519 seed offline. Do not reuse
   `LIVE402_PQ_LOG_SK` from TestNet.
2. Encode the public verifier key with the C2SP vkey format whose key
   name is `402signal.com/pq/log/mainnet-v1`.
3. Store the secret as Fly secret `LIVE402_PQ_LOG_SK_MAINNET` only.
   Code rejects silent fallback to `LIVE402_PQ_LOG_SK`
   (`reuse_testnet_sk=false`).
4. Publish the public half as `LIVE402_PQ_LOG_VKEY_MAINNET`.
   If SK and VKEY are both set, boot requires exact string equality
   with the C2SP vkey derived from the SK (fail closed; env is not
   overwritten on mismatch). If SK is set and VKEY is unset, boot
   writes the derived vkey (ops may stage SK first). If SK is unset,
   there is no MainNet signer.
5. GO checklist: the new MainNet public vkey MUST NOT EQUAL the
   archived TestNet public vkey. Public-only comparison.
   `log_identity.reject_reused_ed25519_vkey` fails the cutover if they
   match. Leave `trust_root.v2.json` `log_signature.vkey` empty until
   the public half is known. Do not commit the secret.

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
