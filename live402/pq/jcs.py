"""RFC 8785 JSON Canonicalization Scheme (I-JSON).

Amounts are decimal strings. One timestamp form (RFC3339 UTC seconds + Z).
Reject NaN, Infinity, duplicate keys, and lone UTF-16 surrogates.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
AMOUNT_KEYS = frozenset(
    {
        "amount",
        "amount_atomic",
        "display_amount",
        "normalized_usd",
        "price",
        "fee",
        "max_price_usd",
        "max_total_cost_usd",
    }
)


class JCSError(ValueError):
    pass


def _reject_duplicates(pairs):
    seen = set()
    out = {}
    for key, val in pairs:
        if key in seen:
            raise JCSError("duplicate key")
        seen.add(key)
        out[key] = val
    return out


def _has_lone_surrogate(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            return True
    return False


def parse(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JCSError("invalid UTF-8") from exc
    else:
        text = raw
    if _has_lone_surrogate(text):
        raise JCSError("lone surrogate")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise JCSError("invalid JSON") from exc


def _reject_constant(name: str):
    raise JCSError("non-finite number")


def canonicalize(obj) -> bytes:
    """Return RFC 8785 UTF-8 bytes. Never emits NaN/Infinity."""
    return _serialize(obj).encode("utf-8")


def canonicalize_text(obj) -> str:
    return _serialize(obj)


def _serialize(obj) -> str:
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        if _has_lone_surrogate(obj):
            raise JCSError("lone surrogate")
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if isinstance(obj, int) and not isinstance(obj, bool):
        return str(int(obj))
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise JCSError("non-finite number")
        return _serialize_number(obj)
    if isinstance(obj, list):
        return "[" + ",".join(_serialize(x) for x in obj) + "]"
    if isinstance(obj, dict):
        items = []
        for key in obj:
            if not isinstance(key, str):
                raise JCSError("object keys must be strings")
            if _has_lone_surrogate(key):
                raise JCSError("lone surrogate")
        for key in sorted(obj.keys(), key=_utf16_key):
            items.append(_serialize(key) + ":" + _serialize(obj[key]))
        return "{" + ",".join(items) + "}"
    raise JCSError("unsupported type")


def _utf16_key(key: str) -> tuple:
    """RFC 8785 sorts by UTF-16 code units."""
    return key.encode("utf-16-be")


def _serialize_number(n: float) -> str:
    # I-JSON / ES6 number serialization (RFC 8785 §3.2.2.3).
    if n == 0:
        return "0"
    return json.dumps(n, ensure_ascii=True)


def require_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not TS_RE.match(text):
        raise JCSError("timestamp must be RFC3339 UTC seconds ending in Z")
    return text


def utc_seconds_z(ts: int | float | None = None) -> str:
    if ts is None:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.fromtimestamp(int(ts), timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def amounts_as_strings(obj):
    """Walk a structure and stringify known amount fields. Reject floats there."""
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if key in AMOUNT_KEYS:
                out[key] = _amount_string(val)
            else:
                out[key] = amounts_as_strings(val)
        return out
    if isinstance(obj, list):
        return [amounts_as_strings(x) for x in obj]
    return obj


def _amount_string(val) -> str:
    if val is None:
        raise JCSError("amount must be a decimal string")
    if isinstance(val, bool):
        raise JCSError("amount must be a decimal string")
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if not math.isfinite(val):
            raise JCSError("non-finite amount")
        return format(val, "f").rstrip("0").rstrip(".") if "." in format(val, "f") else str(int(val))
    text = str(val).strip()
    if not text:
        raise JCSError("amount must be a decimal string")
    try:
        float(text)
    except ValueError as exc:
        raise JCSError("amount must be a decimal string") from exc
    return text
