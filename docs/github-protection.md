# GitHub branch protection

`main` is protected by the active repository ruleset **Protect main**.
This file does not create, edit, or disable GitHub rulesets.

Required CI check name (job id in `.github/workflows/test.yml`):

**`test`**

CodeQL also publishes `analyze (python)` and
`analyze (javascript-typescript)`. Those names appear on the active
ruleset. This PR does not add or remove required checks.

The `test` job installs the universal dependency lock with
`--require-hashes` and runs pip-audit on that same `requirements.txt`.
Do not add a private-signer secret to this public repo.

Remaining human tightening (optional, admin-only, not this PR):

1. Require CODEOWNERS review for the paths in `.github/CODEOWNERS`.
   Reviews are assigned to `@ross402signal`.
2. Restrict who can push to `main` further if the ruleset is loosened.
3. Do not attach deploy secrets to pull-request workflows. `test` and
   CodeQL use `contents: read` only (CodeQL also needs
   `security-events: write` to upload results).

CODEOWNERS is implemented in-tree. Branch protection is already
enabled. This PR does not change the ruleset.
