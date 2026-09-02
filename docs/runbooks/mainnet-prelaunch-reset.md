# Ross-only MainNet pre-launch reset

Audience: Ross only. Do not execute this runbook from CI, Cursor, chat,
or any cloud agent. This PR documents the procedure. It does not
merge, deploy, Fly, or SSH. Do not generate keys. Do not rotate
secrets. Do not print secret values. Do not open a Falcon keyfile.

Production live identity is already `mainnet-v1` /
`402signal.com/pq/log/mainnet-v1` /
`/data/pq-log-mainnet.sqlite` (`fly.toml`). The current MainNet sqlite
is a 2-leaf test tree. The previous MainNet Ed25519 log secret is
compromised and must leave the live env. Falcon broadcasting stays
OFF. No Algorand transaction is part of this reset.

Helpers (public / counts only):

- `scripts/pq_derive_vkey.py` (stdin or `--sk-file`; never prints SK)
- `scripts/pq_public_identity_check.py` (public strings and digests)
- `scripts/pq_log_fresh_state.py` (counts only; optional empty init)

## Preconditions (read this first)

1. You are Ross, on a machine you control, with `umask 077`.
2. Transcript, shell-history, and chat logging are off for secret
   steps. Do not paste a seed, PEM, mnemonic, or Fly secret value
   anywhere.
3. `LIVE402_PQ_FALCON_BROADCAST`, `LIVE402_PQ_FALCON_MAINNET_BROADCAST`,
   and `LIVE402_PQ_FALCON_MAINNET_CANARY` stay unset for the whole
   reset. Unset is the kill switch.
4. Do not run `scripts/pq_mainnet_canary.py --prepare` or `--go`.
5. Do not dial the Falcon signer for a new authorization.
6. Do not copy leaves from `/data/pq-log.sqlite` or the old 2-leaf
   MainNet file into the new tree.
7. Do not put an Ed25519 secret on `402signal-pq-signer-mainnet`.
8. Do not decode or `algokey pq info` the Falcon keyfile for identity
   checks. Public address and digest only.

## A. Confirm broadcast and canary are OFF

Names only. Do not dump process env (that prints secrets).

```
fly secrets list -a 402signal
fly config env -a 402signal
```

Required:

- `LIVE402_PQ_FALCON_BROADCAST` absent from secrets and `[env]`
- `LIVE402_PQ_FALCON_MAINNET_BROADCAST` absent
- `LIVE402_PQ_FALCON_MAINNET_CANARY` absent
- `fly.toml` still has no broadcast or canary keys (already true)

Public confirmation (no txn):

- `GET /transparency` still awaits the first confirmed MainNet checkpoint
- Do not treat AUTHORIZED or SUBMITTED as CONFIRMED

If any broadcast or canary name is present, stop. Unset it (`--stage`)
and do not continue the reset.

## B. Optional offline archive of the old 2-leaf MainNet test DB

Archive is optional and offline. It is not a seed for the new tree.

Prefer stopping log writes first (`LIVE402_PQ_LOG=0` as a kill switch,
or stop the app process). Then snapshot with the SQLite backup API:

```
umask 077
LIVE402_PQ_LOG_DB=/data/pq-log-mainnet.sqlite \
  PYTHONPATH=. python3 scripts/backup_sqlite.py \
  --dest /offline/402signal-mainnet-2leaf-archive
```

Keep the snapshot, a `PRAGMA integrity_check` of `ok`,
`SELECT COUNT(*) FROM leaves` (expect 2), the public origin, and the
software SHA. Store that bundle off-box. Do not HTTP-serve it.

Do not archive `LIVE402_PQ_LOG_SK` or `LIVE402_PQ_LOG_SK_MAINNET`.
A public vkey string (or its sha256 digest) is enough to remember the
compromised identity.

Leave `/data/pq-log.sqlite` (TestNet archive) untouched. See
`docs/pq-testnet-archive.md`.

## C. Retire the old MainNet test DB from the live path

Rename. Do not delete until the new empty file is proven. Do not copy
leaves forward.

```
mkdir -p /data/retired
mv /data/pq-log-mainnet.sqlite \
  /data/retired/pq-log-mainnet-2leaf-<utc-date>.sqlite
# Move -wal / -shm if present. Do not replay them onto a new file.
```

The live path `/data/pq-log-mainnet.sqlite` must now be absent.

## D. Init a fresh empty `/data/pq-log-mainnet.sqlite`

Do not `sqlite3 .dump` the retired file into the new name. Do not
restore the optional archive onto the live path.

Next store boot creates empty schema (0600) at
`/data/pq-log-mainnet.sqlite`. Workstation check of a throwaway copy:

```
LIVE402_PQ_LOG_EPOCH=mainnet-v1 \
  PYTHONPATH=. python3 scripts/pq_log_fresh_state.py \
  --init-empty --dest /tmp/pq-log-mainnet.sqlite
```

On the volume, after the live file exists:

```
PYTHONPATH=. python3 scripts/pq_log_fresh_state.py \
  --db /data/pq-log-mainnet.sqlite
```

Required output: `leaves=0 checkpoints=0 authorized=0 confirmed=0`
and `fresh=yes`. Origin must be `402signal.com/pq/log/mainnet-v1`
once meta is initialized. See step J.

## E. Generate a fresh Ed25519 MainNet log SK locally

Air-gapped or equivalent. `umask 077`. stdin / files only. No CLI secret arguments. No transcript leakage. Boot never generates a key.

```
umask 077
mkdir -p /secure/402signal-mainnet-ed25519
chmod 700 /secure/402signal-mainnet-ed25519
openssl rand -hex 32 > /secure/402signal-mainnet-ed25519/sk.hex
chmod 600 /secure/402signal-mainnet-ed25519/sk.hex
```

Do not `cat`, `less`, `echo`, or paste that file. Do not pass the
hex as a command-line argument. PKCS8 PEM is an allowed encoding in
code (`receipt._parse_log_sk`); if you use PEM, write it to a 0600
file the same way. Do not reuse `LIVE402_PQ_LOG_SK` (TestNet) or the
compromised MainNet seed.

## F. Derive the C2SP vkey for `402signal.com/pq/log/mainnet-v1`

```
PYTHONPATH=. python3 scripts/pq_derive_vkey.py \
  --origin 402signal.com/pq/log/mainnet-v1 \
  --sk-file /secure/402signal-mainnet-ed25519/sk.hex \
  --vkey-out /secure/402signal-mainnet-ed25519/vkey.txt \
  --digest-out /secure/402signal-mainnet-ed25519/vkey.sha256
```

The script refuses a TTY for stdin (use `--sk-file` or a pipe). It
refuses a 32-byte hex token on the command line. It never prints the
seed. stdout is the public vkey unless you pass `--digest-only`.

Record the public vkey and its sha256 digest offline. Those are
public. The seed is not.

## G. Install router SK + MainNet vkey; signer public vkey only

Prefer `--stage` so this checklist does not itself restart machines.
A later authorized human apply/restart is out of scope for this PR.

Router `402signal`:

```
# Public vkey first (still use stdin; keep history clean).
fly secrets set LIVE402_PQ_LOG_VKEY_MAINNET=- \
  --app 402signal --stage \
  < /secure/402signal-mainnet-ed25519/vkey.txt

# Secret seed from the 0600 file. Never NAME=hex on the CLI.
fly secrets set LIVE402_PQ_LOG_SK_MAINNET=- \
  --app 402signal --stage \
  < /secure/402signal-mainnet-ed25519/sk.hex
```

Signer `402signal-pq-signer-mainnet` (public only):

```
fly secrets set LIVE402_PQ_LOG_VKEY=- \
  --app 402signal-pq-signer-mainnet --stage \
  < /secure/402signal-mainnet-ed25519/vkey.txt
```

Confirm with `fly secrets list` (names and Fly digests only):

- Router has `LIVE402_PQ_LOG_SK_MAINNET` and `LIVE402_PQ_LOG_VKEY_MAINNET`
- Signer has `LIVE402_PQ_LOG_VKEY`
- Signer does not have `LIVE402_PQ_LOG_SK` or `LIVE402_PQ_LOG_SK_MAINNET`

Do not `fly ssh console` and print env. Do not `echo $LIVE402_PQ_...`.

After a later authorized apply, `GET /pq/log/trust` must show the new
public vkey, origin `402signal.com/pq/log/mainnet-v1`, Falcon network
`mainnet-v1.0`, `allowed_broadcast=none`, and `not_mainnet_go=true`.

## H. NEVER put the Ed25519 SK on the Falcon signer

`402signal-pq-signer-mainnet` holds the Falcon-1024 keyfile and the
MainNet HMAC token. It authorizes checkpoint transactions. It never
reads BROADCAST. It does not need `LIVE402_PQ_LOG_SK` or
`LIVE402_PQ_LOG_SK_MAINNET`.

If either Ed25519 SK name appears on the signer secret list, unset it
(`--stage`) and treat that as an incident. Do not copy the hex file
onto that machine.

## I. Verify the new vkey is distinct (public / digest only)

Fill the compromised digest from your offline notes. Do not put the
compromised seed in git or chat. Compare public strings and sha256
digests only.

```
PYTHONPATH=. python3 scripts/pq_public_identity_check.py \
  --mainnet-vkey-file /secure/402signal-mainnet-ed25519/vkey.txt \
  --testnet-vkey-file /offline/testnet-vkey.txt \
  --compromised-vkey-digest-file /offline/compromised-mainnet-vkey.sha256
```

Required lines:

- `MAINNET_VKEY_NE_TESTNET ok`
- `MAINNET_VKEY_NE_COMPROMISED ok`
- `identity_check ok`

Code does the same checks (`log_identity.reject_reused_ed25519_vkey`
and `reject_compromised_ed25519_vkey`). Do not compare secrets.

## J. Verify fresh state is empty

Counts only. Do not `SELECT body`, `signed`, or dump the file.

```
PYTHONPATH=. python3 scripts/pq_log_fresh_state.py \
  --db /data/pq-log-mainnet.sqlite
```

Required: `leaves=0 checkpoints=0 authorized=0 confirmed=0 fresh=yes`.

Equivalent SQL (counts only):

```
SELECT COUNT(*) FROM leaves;
SELECT COUNT(*) FROM checkpoints;
SELECT COUNT(*) FROM authorized_anchors;
SELECT COUNT(*) FROM confirmed_anchors;
```

Public HTTP after the authorized apply (empty tree):

- `GET /pq/log/checkpoint` is 404 `no_checkpoint`
- `GET /transparency` still awaits the first confirmed MainNet checkpoint
- Do not append a leaf to "test" this step

## K. No Algorand transaction during reset

Forbidden for the whole reset:

- Setting either BROADCAST flag or MAINNET_CANARY
- `scripts/pq_mainnet_canary.py --prepare` or `--go`
- Dialing the Falcon signer for a new SignedTxn
- Funding, canary POST, or any MainNet (or TestNet) submit
- Treating a later restart as permission to send

Routing and the empty log stay up. Unset broadcast remains the kill
switch. Destroying the Falcon key is not the kill switch.

## L. No copy of old leaves

The new `/data/pq-log-mainnet.sqlite` starts at tree size 0. Do not:

- Restore the 2-leaf archive onto the live path
- Copy `/data/pq-log.sqlite` (TestNet) into the MainNet file
- Replay `-wal` from the retired 2-leaf file
- Insert rows by hand

`docs/pq-recovery.md` drill E still applies: TestNet tree N stays N
on `/data/pq-log.sqlite`. MainNet starts at 0.

## M. Remove the compromised old MainNet Ed25519 SK from the live env

After the new `LIVE402_PQ_LOG_SK_MAINNET` is staged (step G):

```
# If the compromised seed was still the live MainNet SK name, replace
# it by staging the new file (step G) so the Fly digest changes.
fly secrets list -a 402signal
```

Confirm the Fly digest for `LIVE402_PQ_LOG_SK_MAINNET` is not the
digest you recorded for the compromised value. Names and Fly digests
only.

If the compromised seed was ever stored under the TestNet name:

```
fly secrets unset LIVE402_PQ_LOG_SK --app 402signal --stage
```

Do that as part of the TestNet retirement list below, not by printing
the value. Overwrite and then destroy the local compromised seed file
after two offline public-vkey records exist. Do not keep the
compromised seed on the router, the signer, chat, or cloud notes.

## TestNet runtime secrets (checklist only; do not execute here)

Retire these from the **router** live env after MainNet SK/vkey work.
`--stage` only. This PR does not unset them.

| Name | Why retire |
|---|---|
| `LIVE402_PQ_LOG_SK` | TestNet Ed25519 seed. MainNet must not fall back to it. |
| `LIVE402_PQ_LOG_VKEY` | TestNet public vkey on the router. MainNet uses `LIVE402_PQ_LOG_VKEY_MAINNET`. |
| `LIVE402_PQ_SIGNER_TOKEN` | TestNet HMAC. MainNet uses `LIVE402_PQ_SIGNER_MAINNET_TOKEN`. |
| `LIVE402_PQ_FALCON_BROADCAST` | TestNet POST flag. Must stay absent. |
| `LIVE402_PQ_FALCON_MAINNET_BROADCAST` | Must stay absent through this reset. |
| `LIVE402_PQ_FALCON_MAINNET_CANARY` | Must stay absent through this reset. |
| `LIVE402_PQ_SIGNER_ENABLE` | Legacy name. Must stay absent. |

Keep (public, not secrets):

- `LIVE402_PQ_FALCON_MAINNET_ADDRESS` in `fly.toml`
- `LIVE402_PQ_LOG_EPOCH` / `ORIGIN` / `LOG_DB` / `NETWORK=mainnet`
- TestNet archive file `/data/pq-log.sqlite` (do not delete)

Keep on the MainNet signer only: Falcon keyfile, MainNet HMAC token,
public `LIVE402_PQ_LOG_VKEY`. Never the Ed25519 SK.

## MAINNET_FALCON_IDENTITY_DISTINCT (public address / digest only)

Do not open, copy, or decode the Falcon keyfile. Do not run
`algokey pq info --keyfile` for this check. Use the already-published
public addresses:

- TestNet (archived): `OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU`
- MainNet (`fly.toml`): `GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU`

```
PYTHONPATH=. python3 scripts/pq_public_identity_check.py
```

With no vkey flags this still compares the two public Falcon
addresses (MainNet default comes from `fly.toml`). Required:

- `MAINNET_FALCON_IDENTITY_DISTINCT ok`
- Address sha256 digests differ
- `LIVE402_PQ_FALCON_MAINNET_ADDRESS` is not the TestNet string

`log_identity.require_mainnet_identity` now fails closed if the
configured MainNet address equals the documented TestNet public
address.

## After this reset (not this PR)

1. Authorized apply/restart of staged router secrets (human).
2. `GET /pq/log/trust` shows the new public MainNet vkey.
3. Fresh-state counts still zero.
4. First production leaf is a later controlled paid `/route`
   (`docs/pq-first-production-event.md`). Do not fabricate it here.
5. MainNet Falcon canary remains a later GO with both flags off until
   that GO.

## What this runbook is not

- Not permission to merge, deploy, Fly, or SSH from this PR
- Not a MainNet broadcast GO
- Not a Falcon key ceremony (see `docs/pq-key-ceremony.md`)
- Not a seed of TestNet or 2-leaf history into production
- Not an instruction to isolate Ed25519 off the router (still
  in-process on 402signal; Falcon SK isolation is unchanged)
