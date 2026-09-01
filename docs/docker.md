# Docker image notes

The image is `python:3.12.11-slim`. A digest pin is used when the published tag digest is known at build time.

## Non-root

The process stays root in the published Dockerfile.

The Fly volume mounts at `/data`. Catalog, history, and pq-log sqlite files are created there at runtime. A non-root `USER` would not own that mount unless an admin `chown`s the volume on every machine, which is not done today. Switching `USER` without that chown would break production writes.

Blocker: `/data` Fly volume writability for a non-root UID is not proven. Do not add `USER` until an admin confirms a one-time `chown` (or an equivalent volume ACL) and a staging `/ready` check succeeds.
