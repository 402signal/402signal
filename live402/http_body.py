"""Strict HTTP/1.1 request-body framing. Fail closed. Never rfile.read(-1)."""

from __future__ import annotations

import json
import math

from live402 import clock

# 64 KiB hard cap. Callers may pass a smaller max_body.
MAX_BODY = 64 * 1024
BODY_READ_TIMEOUT = 3.0
BODY_DEADLINE_SECONDS = 3.0
# Bound the Content-Length token before int(). 64 KiB is 5 digits.
MAX_CL_TEXT_LEN = 16


class BodyReadError(Exception):
    """Controlled 4xx. Always close the connection; do not keep-alive."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = int(status)
        self.error = str(error)
        self.close = True


def reject_json_constant(value):
    """json.loads parse_constant: NaN / Infinity must not become numbers."""
    raise ValueError("non-finite JSON constant")


def _header_values(headers, name: str) -> list[str]:
    """Every value for a header. Prefer get_all(); never only get()."""
    if headers is None:
        return []
    getter = getattr(headers, "get_all", None)
    raw: list = []
    if callable(getter):
        found = getter(name)
        if found:
            raw.extend(found)
    else:
        single = headers.get(name) if hasattr(headers, "get") else None
        if single is not None:
            raw.append(single)
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        for part in text.split(","):
            piece = part.strip()
            if piece:
                out.append(piece)
    return out


def transfer_encoding_present(headers) -> bool:
    """True if Transfer-Encoding is present at all, including a blank value."""
    if headers is None:
        return False
    getter = getattr(headers, "get_all", None)
    if callable(getter):
        found = getter("Transfer-Encoding")
        if found is None:
            return False
        if isinstance(found, (list, tuple)):
            return len(found) > 0
        return True
    if hasattr(headers, "get"):
        return headers.get("Transfer-Encoding") is not None
    return False


def parse_content_length_token(raw: str) -> int:
    """ASCII decimal only. Reject signed, empty, non-digit, overflow, and bombs."""
    text = str(raw)
    if not text or not text.isascii() or not text.isdigit():
        raise BodyReadError(400, "invalid Content-Length")
    if len(text) > MAX_CL_TEXT_LEN:
        raise BodyReadError(413, "body too large")
    try:
        n = int(text)
    except (ValueError, OverflowError):
        raise BodyReadError(400, "invalid Content-Length") from None
    if n < 0:
        raise BodyReadError(400, "invalid Content-Length")
    return n


def declared_content_length(headers) -> int:
    """Exactly one usable Content-Length. Duplicates, TE, and conflicts fail closed."""
    if transfer_encoding_present(headers):
        raise BodyReadError(400, "Transfer-Encoding is not allowed")
    values = _header_values(headers, "Content-Length")
    if not values:
        raise BodyReadError(400, "Content-Length required")
    parsed: list[int] = []
    for token in values:
        parsed.append(parse_content_length_token(token))
    if len(values) != 1:
        raise BodyReadError(400, "ambiguous Content-Length")
    return parsed[0]


def read_exactly(rfile, n: int, deadline: float | None = None) -> bytes:
    """Read exactly n bytes. Short body or deadline fails. Never read(-1)."""
    if n < 0:
        raise BodyReadError(400, "invalid Content-Length")
    if n == 0:
        return b""
    buf = bytearray()
    remaining = n
    while remaining > 0:
        if deadline is not None and clock.monotonic() >= deadline:
            raise BodyReadError(408, "body read timeout")
        try:
            chunk = rfile.read(remaining)
        except Exception as exc:
            raise BodyReadError(400, "short body") from exc
        if not chunk:
            raise BodyReadError(400, "short body")
        buf.extend(chunk)
        remaining -= len(chunk)
    if deadline is not None and clock.monotonic() >= deadline:
        raise BodyReadError(408, "body read timeout")
    return bytes(buf)


def loads_json_object(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_json_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        raise BodyReadError(400, "invalid JSON") from None
    if not isinstance(payload, dict) or isinstance(payload, bool):
        raise BodyReadError(400, "JSON object required")
    return payload


def _finite_float(text: str) -> float:
    n = float(text)
    if not math.isfinite(n):
        raise ValueError("non-finite JSON number")
    return n


def read_json_object(handler, max_body: int = MAX_BODY, deadline: float | None = None) -> dict:
    """Shared POST reader. On error: BodyReadError and caller must close."""
    cap = int(max_body) if max_body else MAX_BODY
    if cap < 1:
        cap = MAX_BODY
    length = declared_content_length(handler.headers)
    if length > cap:
        raise BodyReadError(413, "body too large")
    if deadline is None:
        deadline = clock.monotonic() + BODY_DEADLINE_SECONDS
    if clock.monotonic() >= deadline:
        raise BodyReadError(408, "body read timeout")
    raw = read_exactly(handler.rfile, length, deadline=deadline)
    return loads_json_object(raw)
