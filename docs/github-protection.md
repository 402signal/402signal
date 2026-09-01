# GitHub branch protection (human checklist)

This repository cannot set branch protection through the GitHub API from this PR. Do not claim protection is on until a repo admin completes the steps below.

Required CI check name (job id in `.github/workflows/test.yml`):

**`test`**

1. GitHub → Settings → Rules → Rulesets (or Branches → Branch protection) for `main`.
2. Require a pull request before merging.
3. Require status checks to pass: add exactly `test`.
4. Do not require checks that only exist on `pull_request_target` (that trigger is not used).
5. Restrict who can push to `main`.
6. Require CODEOWNERS review for the paths in `.github/CODEOWNERS` once the `@402signal/maintainers` team exists.
7. Do not attach deploy secrets to pull-request workflows. `test` and CodeQL use `contents: read` only (CodeQL also needs `security-events: write` to upload results). The `test` job also runs pip-audit on `requirements.txt`. Do not add a private-signer secret to this public repo.
8. Confirm CODEOWNERS team membership before enforcing reviews.

CODEOWNERS is implemented in-tree. Branch protection is **human**, not implemented by this change.
