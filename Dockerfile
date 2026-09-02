# Pin the official multi-arch index digest from:
#   docker buildx imagetools inspect python:3.12.14-slim
# (This environment had no docker CLI. Digest confirmed from Docker Hub
#  tag API + registry-1.docker.io Docker-Content-Digest. Same value.)
# Digest: sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc
# Tag python:3.12.14-slim includes gh-150743 (outbound http.client
# interim-1xx / chunked-trailer DoS limit). 3.12.11 does not.
# linux/amd64 platform manifest under that index:
#   sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79
FROM python:3.12.14-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

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
