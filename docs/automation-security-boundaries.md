# Automation security boundaries

This is the public 402Signal router repository (`402signalhq/402signal`).
It is not `402signal-pq-signer`. These rules apply to bots, cloud
agents, and humans acting through those tools.

This PR documents boundaries only. It does not deploy. It does not
touch Fly. It does not isolate workstation credentials. It does not
change GitHub rulesets.

## Roles

### 402dev

Code, pull requests, and local plus CI tests only.

- May edit this repo on a feature branch and open a PR.
- May run `LIVE402_FIXTURE=1` unittest and other local fixtures.
- Must not use production Fly.
- Must not read, print, set, or rotate secrets.
- Must not deploy.
- Must not hold MainNet transaction authority.
- Must not run live MainNet `--prepare` or `--go`.

### 402security

Threat model and exact-diff review only.

- Reviews the precise patch and residual risk.
- May request changes or withhold GO.
- Must not receive secrets, mnemonics, wallet files, or Fly tokens.
- Must not mutate production.
- Must not deploy.
- GO is not a deploy, not a secret set, and not a MainNet send.

### 402QA

Black-box public interfaces plus local fixtures.

- May hit public HTTPS endpoints and local fixture servers.
- May use published OpenAPI, MCP, and HTML pages.
- Must not `fly ssh` or `fly console`.
- Must not read or set secrets.
- Must not deploy.
- Must not inspect production process environment.

### 402Website

Website, UI, and content only.

- May change `live402/static/`, presentation copy, and related tests.
- Must not perform Fly operations.
- Must not read or set secrets.
- Must not deploy.
- Must not treat a content PR as production mutation.

### Ross / 402ops

The only production-mutation role.

- Deploy.
- Fly secrets (`fly secrets set` / `unset`) after 402security GO where
  required.
- Tightly controlled SSH. Not general console use. Not environment
  dumps.
- MainNet key ceremony (see `docs/pq-key-ceremony.md`).
- Canary authorization. Live MainNet `--prepare` / `--go` only from
  this role, never from a bot.
- Rollback.

Bots are not Ross / 402ops even when they can open a PR.

## Production prohibitions (bots)

Bots and cloud agents must not run these against production, Fly
machines, or live MainNet paths:

- `printenv`
- `env`
- `set` as an environment dump
- `/proc/*/environ` dumps
- general `fly ssh`
- general `fly console`
- `fly secrets set`
- `fly secrets unset`
- MainNet `--prepare`
- MainNet `--go`

Default, `--summary-only`, and fixture-mode canary reads stay
documentation-only here. A bot still must not invoke live MainNet
`--prepare` or `--go`.

Do not dump process environment to prove a secret is unset. Absence
is shown by not holding the value, not by printing `environ`.

## Credential isolation (document only)

Bots must not use Ross's normal Fly or GitHub credential directories.

Eventual target: a separate OS user or container whose home does not
contain operator credentials. That environment must not mount or copy:

- `~/.fly`
- `~/.config/gh`
- `~/.config/gh-ross`
- SSH private keys
- wallet or key files
- mnemonic backups

This PR does not implement that isolation. Do not create those
directories, copy operator tokens, or "fix" local credentials as part
of a bot change. Until isolation exists, bots stay on repo-scoped PR
credentials only and treat any operator Fly, GitHub, SSH, or wallet
material as out of bounds.

## Scope reminder

Allowed without Ross / 402ops: feature-branch code, docs, CODEOWNERS
text, and fixture CI.

Forbidden without Ross / 402ops: Fly, deploy, secrets, SSH, MainNet
send, and signer-repo access.
