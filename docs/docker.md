# Docker image notes

Base image is the official `python:3.12.11-slim` tag pinned to the
multi-arch index digest from:

```
docker buildx imagetools inspect python:3.12.11-slim
```

Recorded digest (index):

`sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f`

Dockerfile:

`FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f`

linux/amd64 platform manifest under that index:
`sha256:0b29ab9e420820f53d1cd5ce0157dfe07bea8a7cff5b4754d6d95c07b0e5bc47`

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
