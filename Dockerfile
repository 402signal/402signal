# Pin a current 3.12 patch. Digest omitted here because Docker Hub
# tags move; pin the patch so Fly/Linux rebuilds stay reproducible enough
# without a lockfile. See docs/docker.md.
FROM python:3.12.11-slim

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
