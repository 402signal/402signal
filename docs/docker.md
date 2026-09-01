# Docker image notes

Base image is the official `python:3.12.14-slim` tag pinned to the
multi-arch index digest.

Chosen release: **Python 3.12.14**. Why: it is the 3.12 security
release that includes **gh-150743** (GHSA-w4q2-g22w-6fr4). Outbound
`http.client` now limits chunked-response trailer lines and interim
(1xx) responses to 100 each and raises `HTTPException` past either
limit. A malicious seller can no longer stream `100 Continue` or
trailers forever and hang a probe even when a socket timeout is set.

`python:3.12.11-slim` does **not** contain that fix. Pinning 3.12.11
and bounding inbound `http_body` reads does not close this P1.

```
docker buildx imagetools inspect python:3.12.14-slim
```

This cloud environment had no docker CLI (`docker buildx imagetools
inspect` is the intended pin command). The same official index
digest was read from the registry APIs that imagetools uses:

1. Docker Hub `library/python` tag `3.12.14-slim` (`digest` field)
2. `registry-1.docker.io` `Docker-Content-Digest` for
   `manifests/3.12.14-slim` (OCI image index)

Reconfirmed 2026-09-01. Both returned:

`sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc`

Dockerfile:

`FROM python:3.12.14-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc`

linux/amd64 platform manifest under that index:
`sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79`

Do not invent a digest. Do not roll back to 3.12.11.

## Hash-locked requirements

Evaluated `pip hash` / `--require-hashes` for `cryptography==50.0.1`.

`cryptography` publishes many platform wheels (manylinux, musllinux,
macosx, win) plus transitive `cffi` / `pycparser` wheels. A
`--require-hashes` lock that lists only the linux/amd64 set Fly uses
would fail `pip install` on other developer platforms. A lock that lists
every published wheel is large and still drifts when Warehouse adds a
new wheel for the same version.

Decision: do not add `requirements.lock` or `--require-hashes` to the
Dockerfile. Keep the exact direct pin `cryptography==50.0.1` in
`requirements.txt`. That is the same reproducibility we already had,
plus the base-image digest pin. Do not loosen the cryptography pin.

## Non-root (ACCEPTED path, not implemented)

The process stays root in the published Dockerfile.

The Fly volume mounts at `/data`. Catalog, history, and pq-log sqlite
files are created there at runtime. A non-root `USER` would not own that
mount unless an admin `chown`s the volume on every machine, which is not
done today. Switching `USER` without that chown would break production
writes.

A root-then-drop-privs entrypoint was considered. It cannot be proven in
this environment against the production Fly volume without touching that
volume. Do not chown production `/data` from this PR.

Blocker: `/data` Fly volume writability for a non-root UID is not
proven. Do not add `USER` until an admin confirms a one-time `chown`
(or an equivalent volume ACL) and a staging `/ready` check succeeds.
