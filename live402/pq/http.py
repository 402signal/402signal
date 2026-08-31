"""C2SP read APIs under /pq/log (no trailing slash on the origin).

GET /pq/log/checkpoint  text/plain; charset=utf-8
GET /pq/log/tile/{L}/{N} and .p/{W}  application/octet-stream
GET /pq/log/tile/entries/{N} and .p/{W}  application/octet-stream

Height 8 is implicit. Not sumdb /tile/H/L/N.
"""

from __future__ import annotations

from live402.pq import HTTP_PREFIX
from live402.pq import store
from live402.pq import tiles as tilemod

CHECKPOINT_TYPE = "text/plain; charset=utf-8"
TILE_TYPE = "application/octet-stream"


def is_log_path(path: str) -> bool:
    raw = (path or "").split("?", 1)[0]
    return raw == HTTP_PREFIX or raw.startswith(HTTP_PREFIX + "/")


def handle(path: str) -> tuple[int, bytes, str, dict]:
    """Return (status, body, content_type, extra_headers)."""
    raw = (path or "").split("?", 1)[0]
    if raw != HTTP_PREFIX and not raw.startswith(HTTP_PREFIX + "/"):
        return 404, b'{"error": "not found"}', "application/json; charset=utf-8", {}
    rel = raw[len(HTTP_PREFIX) :].lstrip("/")
    if rel == "checkpoint":
        note = store.latest_checkpoint()
        if not note:
            return 404, b'{"error": "no_checkpoint"}', "application/json; charset=utf-8", {
                "Cache-Control": "no-store"
            }
        data = note.encode("utf-8")
        return 200, data, CHECKPOINT_TYPE, {"Cache-Control": "no-store"}
    if rel.startswith("tile/"):
        try:
            parsed = tilemod.parse_tile_relpath(rel)
        except ValueError:
            return 404, b'{"error": "not found"}', "application/json; charset=utf-8", {}
        if parsed["kind"] == "entries":
            blob = store.get_entry_bundle(parsed["n"], parsed["width"])
        else:
            blob = store.get_tile(parsed["level"], parsed["n"], parsed["width"])
        if blob is None:
            return 404, b'{"error": "not found"}', "application/json; charset=utf-8", {}
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return 200, blob, TILE_TYPE, headers
    return 404, b'{"error": "not found"}', "application/json; charset=utf-8", {}
