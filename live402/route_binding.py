"""Opt-in proof-carrying routes. Evidence of observed terms, never spend authority.

The v1 quote profile hashes the entire observed x402 v2 envelope. It intentionally
rejects floats, unsafe integers, redirects and ambiguous JSON. Dynamic envelopes
may fail to match; no field is silently ignored to improve compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from urllib.parse import urlsplit

from live402 import payment
from live402.pq import jcs

MODEL = "proof_carrying_route_v1"
MAX_JSON_BYTES = 64 * 1024
MAX_DEPTH = 24
MAX_SAFE_INTEGER = 2**53 - 1
MAX_TTL = 120
HEX = re.compile(r"[0-9a-f]{64}\Z")


class BindingError(ValueError):
    """Only coarse, constant reason codes. Never echo seller/request data."""


def _fail(reason="invalid_binding"):
    raise BindingError(reason)


def strict_json(raw: str | bytes):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_JSON_BYTES:
        _fail("invalid_json")
    try:
        if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_JSON_BYTES:
            _fail("invalid_json")
        value = jcs.parse(raw)
        # Limit both source size and parsed nesting (before further processing).
        _walk(value, floats=True)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("invalid_json")


def _walk(value, depth=0, *, floats=False):
    if depth > MAX_DEPTH:
        _fail("invalid_json")
    if type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            _fail("invalid_json")
        return
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= MAX_SAFE_INTEGER:
        return
    if (
        floats
        and type(value) is float
        and math.isfinite(value)
        and abs(value) <= MAX_SAFE_INTEGER
    ):
        return
    if type(value) is list:
        for item in value:
            _walk(item, depth + 1, floats=floats)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for key, item in value.items():
            _walk(key, depth + 1, floats=floats)
            _walk(item, depth + 1, floats=floats)
        return
    _fail("unsupported_json_value")


def canonical(value) -> bytes:
    """RFC8785 subset with safe integers only; identical in Python and JS."""
    try:
        _walk(value)
        raw = jcs.canonicalize(value)
        if len(raw) > MAX_JSON_BYTES:
            _fail("invalid_json")
        return raw
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("invalid_json")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def request_context(url: str, method: str, body: bytes = b"") -> dict:
    if type(url) is not str or len(url) > 4096 or not url.isascii():
        _fail("unsupported_resource")
    try:
        parsed = urlsplit(url)
        if (
            not url.startswith("https://")
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or "#" in url
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
            or "\\" in url
            or parsed.port not in (None, 443)
        ):
            _fail("unsupported_resource")
    except ValueError:
        _fail("unsupported_resource")
    if method not in ("GET", "POST") or type(body) is not bytes:
        _fail("unsupported_resource")
    if len(body) > MAX_JSON_BYTES or (method == "GET" and body):
        _fail("unsupported_resource")
    return {
        "url": url,
        "method": method,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def observed_challenge(status, headers: dict, body: bytes) -> dict:
    """No lossy JSON path: reject duplicate keys and disagreeing wire channels."""
    if type(status) is not int or status != 402:
        _fail("not_402")
    candidates = []
    for name in ("payment-required", "x-payment-required"):
        if name in headers:
            val = headers[name]
            try:
                if type(val) is not str or len(val) > MAX_JSON_BYTES:
                    _fail("invalid_json")
                decoded = base64.b64decode(val, validate=True)
                if base64.b64encode(decoded).decode("ascii") != val:
                    _fail("invalid_json")
            except (ValueError, TypeError):
                _fail("invalid_json")
            candidates.append(strict_json(decoded))
    if body:
        try:
            val = strict_json(body)
        except BindingError:
            # A non-JSON error page alongside a header cannot prove equivalence.
            _fail("invalid_json")
        if type(val) is dict and ("accepts" in val or "x402Version" in val):
            candidates.append(val)
    if not candidates or any(
        canonical(c) != canonical(candidates[0]) for c in candidates
    ):
        _fail("ambiguous_challenge")
    validate_envelope(candidates[0])
    return candidates[0]


def validate_envelope(env: dict) -> None:
    canonical(env)
    if (
        type(env) is not dict
        or type(env.get("x402Version")) is not int
        or env["x402Version"] != 2
    ):
        _fail("unsupported_challenge")
    # New protocol extensions need explicit review before this guard can attest
    # to their meaning. Known extension data still participates in the full hash.
    if set(env) - {"x402Version", "accepts", "resource", "error", "extensions"}:
        _fail("unsupported_challenge")
    exts = env.get("extensions", {})
    if type(exts) is not dict or set(exts) - {"bazaar"}:
        _fail("unsupported_extension")
    accepts = env.get("accepts")
    if type(accepts) is not list or not 1 <= len(accepts) <= 32:
        _fail("unsupported_challenge")
    for acc in accepts:
        if type(acc) is not dict:
            _fail("unsupported_challenge")
    resource = env.get("resource")
    if resource is not None and (
        type(resource) is not dict or set(resource) - {"url", "description", "mimeType"}
    ):
        _fail("unsupported_resource")


def selected_index(env, selected) -> int:
    validate_envelope(env)
    if type(selected) is not dict:
        _fail("invalid_selected_payment")
    matches = []
    for i, acc in enumerate(env["accepts"]):
        if payment.selected_payment_matches_current_envelope(
            selected, {"envelope": {**env, "accepts": [acc]}}
        ):
            matches.append(i)
    if len(matches) != 1:
        _fail("ambiguous_selected_payment")
    acc = env["accepts"][matches[0]]
    if set(acc) - {
        "scheme",
        "network",
        "amount",
        "asset",
        "currency",
        "payTo",
        "maxTimeoutSeconds",
        "extra",
    }:
        _fail("unsupported_challenge")
    if (
        acc.get("scheme") != "exact"
        or type(acc.get("maxTimeoutSeconds")) is not int
        or acc["maxTimeoutSeconds"] <= 0
    ):
        _fail("unsupported_challenge")
    # maxTimeoutSeconds is an authorization timeout, NOT a quote expiration.
    return matches[0]


def ttl_seconds() -> int:
    raw = os.environ.get("LIVE402_ROUTE_BINDING_TTL_S", "60")
    if not re.fullmatch(r"[1-9][0-9]*", raw) or not 1 <= int(raw) <= MAX_TTL:
        _fail("invalid_binding_config")
    return int(raw)


def build(result: dict, body: dict, *, now: int | None = None) -> dict:
    """Called only after the ordinary final billable-winner gate, before settle."""
    from live402 import route, select

    if not route._billable_winner(body, 200, result):
        _fail("invalid_winner")
    allowed = set(select.EXPLICIT_CONSTRAINT_KEYS) | {"need", "url", "policy"}
    if set(body) - allowed or result.get("unresolved_constraints"):
        _fail("unresolved_policy")
    obs = result.get("binding_observation")
    if type(obs) is not dict or set(obs) != {"request", "observed_at", "quote_sha256"}:
        _fail("unproven_observation")
    ctx = obs["request"]
    validate_context(ctx)
    if ctx["url"] != result.get("url"):
        _fail("resource_changed")
    env = result["envelope"]
    index = selected_index(env, result["selected_payment"])
    if obs["quote_sha256"] != digest(env):
        _fail("quote_changed")
    resource = env.get("resource") or {}
    if resource.get("url", ctx["url"]) != ctx["url"]:
        _fail("resource_changed")
    current = int(time.time()) if now is None else now
    observed = obs["observed_at"]
    if type(observed) is not int or not observed <= current < observed + ttl_seconds():
        _fail("quote_expired")
    return {
        "model": MODEL,
        "observed_at": observed,
        "expires_at": observed + ttl_seconds(),
        "request": ctx.copy(),
        "quote_sha256": obs["quote_sha256"],
        "selected_index": index,
    }


def validate_context(ctx):
    if type(ctx) is not dict or set(ctx) != {"url", "method", "body_sha256"}:
        _fail()
    request_context(ctx["url"], ctx["method"])
    if type(ctx["body_sha256"]) is not str or not HEX.fullmatch(ctx["body_sha256"]):
        _fail()


def validate(binding):
    canonical(binding)
    if type(binding) is not dict or set(binding) != {
        "model",
        "observed_at",
        "expires_at",
        "request",
        "quote_sha256",
        "selected_index",
    }:
        _fail()
    if binding["model"] != MODEL:
        _fail()
    validate_context(binding["request"])
    observed, expiry = binding["observed_at"], binding["expires_at"]
    if (
        type(observed) is not int
        or type(expiry) is not int
        or not 0 <= observed < expiry <= observed + MAX_TTL
    ):
        _fail()
    if type(binding["quote_sha256"]) is not str or not HEX.fullmatch(
        binding["quote_sha256"]
    ):
        _fail()
    if (
        type(binding["selected_index"]) is not int
        or not 0 <= binding["selected_index"] < 32
    ):
        _fail()
    return binding


def verify_challenge(
    binding,
    *,
    status: int,
    envelope: dict,
    url: str,
    method: str,
    body: bytes = b"",
    now: int | None = None,
) -> dict:
    """Pure comparison; callers must authenticate the binding with verify_route first."""
    validate(binding)
    current = int(time.time()) if now is None else now
    if (
        type(current) is not int
        or not binding["observed_at"] <= current < binding["expires_at"]
    ):
        _fail("quote_expired")
    if type(status) is not int or status != 402:
        _fail("not_402")
    if request_context(url, method, body) != binding["request"]:
        _fail("resource_changed")
    validate_envelope(envelope)
    if digest(envelope) != binding["quote_sha256"]:
        _fail("quote_changed")
    index = binding["selected_index"]
    if index >= len(envelope["accepts"]):
        _fail()
    return json.loads(canonical(envelope["accepts"][index]))


def verify_route(
    result: dict,
    expected_request: dict,
    *,
    vkey: str,
    status: int,
    envelope: dict,
    url: str,
    method: str,
    body: bytes = b"",
    now=None,
) -> dict:
    """Authenticate committed evidence with a caller-pinned key, then compare terms."""
    from live402.pq import receipt

    if not vkey:
        _fail("untrusted_receipt")
    try:
        tr = result["pq_trust"]["transparency"]
        reveal = tr["reveal"]
        if reveal["event_version"] != "402signal.route_decision.v4":
            _fail("unsupported_receipt")
        receipt.verify_route_receipt(tr["receipt"], reveal, vkey)
        evidence = reveal["evidence"]
        _walk(expected_request, floats=True)
        if not _request_equal(strict_json(evidence["request_json"]), expected_request):
            _fail("request_mismatch")
        binding = evidence["binding"]
        if result.get("decision_binding") != binding:
            _fail("binding_mismatch")
        return verify_challenge(
            binding,
            status=status,
            envelope=envelope,
            url=url,
            method=method,
            body=body,
            now=now,
        )
    except (KeyError, TypeError, ValueError, receipt.ReceiptError) as exc:
        if isinstance(exc, BindingError):
            raise
        _fail("untrusted_receipt")


def _request_equal(left, right):
    """JSON value equality: numbers are equivalent, booleans never equal numbers.

    The signed JSON string stays opaque for commitment verification. Comparison
    of caller policy uses JSON semantics (1 and 1.0), consistent with JavaScript.
    Both inputs must already have passed the bounded JSON profile.
    """
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _request_equal(left[k], right[k]) for k in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _request_equal(a, b) for a, b in zip(left, right)
        )
    return left == right
