# Pin the official multi-arch index digest from:
#   docker buildx imagetools inspect python:3.12.11-slim
# Digest: sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f
# Tag python:3.12.11-slim kept as a comment for humans.
FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fingerprint static assets from the git SHA of this build.
# Pass --build-arg GIT_SHA=$(git rev-parse HEAD) when available.
# If omitted, record SHA from the copied .git/HEAD + refs (see .dockerignore).
# Runtime never reads FLY_IMAGE_REF and does not expose a public SHA endpoint.
ARG GIT_SHA=
RUN set -eu; \
    sha="${GIT_SHA}"; \
    if [ -z "${sha}" ] && [ -f /app/.git/HEAD ]; then \
      ref="$(cat /app/.git/HEAD)"; \
      case "${ref}" in \
        ref:*) \
          refpath="/app/.git/${ref#ref: }"; \
          if [ -f "${refpath}" ]; then \
            sha="$(tr -d '[:space:]' < "${refpath}")"; \
          elif [ -f /app/.git/packed-refs ]; then \
            sha="$(awk -v r="${ref#ref: }" '$2==r {print $1; exit}' /app/.git/packed-refs)"; \
          fi ;; \
        *) sha="$(printf '%s' "${ref}" | tr -d '[:space:]')" ;; \
      esac; \
    fi; \
    if [ -n "${sha}" ]; then printf '%s\n' "${sha}" > /app/.asset-version; fi
ENV LIVE402_ASSET_VERSION=${GIT_SHA}

ENV LIVE402_HOST=0.0.0.0
ENV PORT=8080
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Stays root. /data Fly volume writability for a non-root UID is not proven;
# adding USER would break production sqlite writes. See docs/docker.md.

# Fly sets PORT. LIVE402_HOST defaults to 0.0.0.0 here.
CMD ["sh", "-c", "python3 -m live402 --host ${LIVE402_HOST:-0.0.0.0} --port ${PORT:-8080}"]
