"""C2SP read APIs under /pq/log (no trailing slash on the origin).

GET /pq/log/checkpoint  text/plain; charset=utf-8  (current / latest)
GET /pq/log/checkpoint/{tree_size}  signed checkpoint for that tree size
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
# Inclusive C2SP / SQLite INTEGER range. 1 <= n <= 2^63-1.
MAX_TREE_SIZE = 9223372036854775807
_MAX_TREE_DIGITS = 19


def parse_checkpoint_tree_size(rel: str) -> tuple[bool, int | None]:
    """Parse checkpoint/{tree_size}.

    Returns (is_sized_path, n). n is None when the path is a sized checkpoint
    request that must 404 without touching the checkpoint table: 0, leading
    zero, sign, whitespace, junk, overflow, or more than 19 digits.
    """
    prefix = "checkpoint/"
    if not rel.startswith(prefix):
        return False, None
    rest = rel[len(prefix) :]
    if not rest or "/" in rest:
        return True, None
    if not rest.isdigit() or rest.startswith("0"):
        return True, None
    if len(rest) > _MAX_TREE_DIGITS:
        return True, None
    n = int(rest)
    if n < 1 or n > MAX_TREE_SIZE:
        return True, None
    return True, n


def _checkpoint_tree_size(rel: str) -> int | None:
    """Parse checkpoint/{tree_size}. None if not a valid sized path."""
    is_sized, n = parse_checkpoint_tree_size(rel)
    if not is_sized:
        return None
    return n


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
    is_sized, sized = parse_checkpoint_tree_size(rel)
    if is_sized:
        missing = {"Cache-Control": "no-store"}
        not_found = (404, b'{"error": "no_checkpoint"}', "application/json; charset=utf-8", missing)
        if sized is None:
            return not_found
        current = int(store.size() or 0)
        if sized > current:
            return not_found
        note = store.checkpoint_at(sized)
        if not note:
            return not_found
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
