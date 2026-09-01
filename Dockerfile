# Pin the official multi-arch index digest from:
#   docker buildx imagetools inspect python:3.12.11-slim
# Digest: sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f
# Tag python:3.12.11-slim kept as a comment for humans.
FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV LIVE402_HOST=0.0.0.0
ENV PORT=8080
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Stays root. /data Fly volume writability for a non-root UID is not proven;
# adding USER would break production sqlite writes. See docs/docker.md.

# Fly sets PORT. LIVE402_HOST defaults to 0.0.0.0 here.
CMD ["sh", "-c", "python3 -m live402 --host ${LIVE402_HOST:-0.0.0.0} --port ${PORT:-8080}"]
