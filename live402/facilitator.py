"""Facilitator verify + settle. No private payment keys. Fail closed."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from live402 import cdp_auth, payment

# Official x402 + CDP REST (2026):
# https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/verify-payment
# https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/settle-payment
# https://docs.cdp.coinbase.com/x402/core-concepts/facilitator
CDP_FACILITATOR_BASE = "https://api.cdp.coinbase.com/platform/v2/x402"
CDP_VERIFY_URL = CDP_FACILITATOR_BASE + "/verify"
CDP_SETTLE_URL = CDP_FACILITATOR_BASE + "/settle"

# https://facilitator.payai.network/  POST /verify and POST /settle
PAYAI_VERIFY_URL = payment.SOLANA_FACILITATOR.rstrip("/") + "/verify"
PAYAI_SETTLE_URL = payment.SOLANA_FACILITATOR.rstrip("/") + "/settle"

# https://facilitator.goplausible.xyz/docs  POST /verify and POST /settle
GOPLAUSIBLE_VERIFY_URL = payment.ALGORAND_FACILITATOR.rstrip("/") + "/verify"
GOPLAUSIBLE_SETTLE_URL = payment.ALGORAND_FACILITATOR.rstrip("/") + "/settle"

USER_AGENT = "402Signal/0.1 (x402 resource server; no payment keys)"
# Caps only. Paid /route uses remaining deadline, not these as sequential budgets.
VERIFY_TIMEOUT = 8.0
SETTLE_TIMEOUT = 10.0
MAX_BODY = 64 * 1024

ALLOWLISTED_URLS = frozenset(
    {
        CDP_VERIFY_URL,
        CDP_SETTLE_URL,
        PAYAI_VERIFY_URL,
        PAYAI_SETTLE_URL,
        GOPLAUSIBLE_VERIFY_URL,
        GOPLAUSIBLE_SETTLE_URL,
    }
)
ALLOWLISTED_HOSTS = frozenset(
    {
        "api.cdp.coinbase.com",
        "facilitator.payai.network",
        "facilitator.goplausible.xyz",
    }
)
EXPECTED_PATHS = frozenset({"/platform/v2/x402/verify", "/platform/v2/x402/settle", "/verify", "/settle"})


@dataclass
class FacilitatorResult:
    ok: bool
    body: dict = field(default_factory=dict)
    error: str = ""
    url: str = ""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Explicit no-redirect. Never follow, never forward Authorization."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoints_for(rail: str) -> tuple[str, str]:
    if rail == "solana":
        return PAYAI_VERIFY_URL, PAYAI_SETTLE_URL
    if rail == "algorand":
        return GOPLAUSIBLE_VERIFY_URL, GOPLAUSIBLE_SETTLE_URL
    if rail == "base":
        return CDP_VERIFY_URL, CDP_SETTLE_URL
    return "", ""


def facilitator_url_allowed(url: str) -> bool:
    """Exact HTTPS allowlisted host + fixed endpoint. No credentials. No caller URL."""
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower()
    if host not in ALLOWLISTED_HOSTS:
        return False
    path = parsed.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    if path not in EXPECTED_PATHS:
        return False
    if parsed.query or parsed.fragment:
        return False
    canonical = "%s://%s%s" % (parsed.scheme, host, path)
    if parsed.port and parsed.port != 443:
        return False
    return canonical in ALLOWLISTED_URLS


def _auth_headers(rail: str, method: str, url: str) -> dict[str, str] | None:
    """Headers or None if this rail cannot be called (fail closed)."""
    if rail == "base":
        token = cdp_auth.bearer_for(method, url)
        if not token:
            return None
        return {"Authorization": "Bearer " + token}
    if rail == "solana":
        token = (
            os.environ.get("PAYAI_ACCESS_TOKEN")
            or os.environ.get("PAYAI_API_KEY")
            or ""
        ).strip()
        if token:
            return {"Authorization": "Bearer " + token}
    return {}


def _read_capped(fp) -> bytes:
    if fp is None:
        return b""
    try:
        raw = fp.read(MAX_BODY + 1)
    except Exception:
        return b""
    if raw is None:
        return b""
    if len(raw) > MAX_BODY:
        return raw[:MAX_BODY]
    return raw


def _payload_from_bytes(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {"error": "invalid_facilitator_response"}
    if not isinstance(payload, dict):
        return {"error": "invalid_facilitator_response"}
    return payload


def post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 8.0):
    """POST JSON to an allowlisted facilitator. Tests patch this. Returns (status, payload_dict)."""
    if not facilitator_url_allowed(url):
        return None, {"error": "invalid_facilitator_url"}
    raw = json.dumps(body).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    for key, val in (headers or {}).items():
        if val:
            hdrs[key] = val
    req = urllib.request.Request(url, data=raw, method="POST", headers=hdrs)
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            text_bytes = _read_capped(resp)
    except urllib.error.HTTPError as err:
        status = err.code
        try:
            text_bytes = _read_capped(err)
        except Exception:
            text_bytes = b""
    except Exception:
        return None, {"error": "facilitator_unavailable"}
    payload = _payload_from_bytes(text_bytes)
    return status, payload


def _call(rail: str, url: str, body: dict, timeout: float) -> FacilitatorResult:
    if not facilitator_url_allowed(url):
        return FacilitatorResult(ok=False, error="invalid_facilitator_url", url=url)
    headers = _auth_headers(rail, "POST", url)
    if headers is None:
        return FacilitatorResult(ok=False, error="cdp_auth_not_configured", url=url)
    status, payload = post_json(url, body, headers=headers, timeout=timeout)
    if status is None:
        return FacilitatorResult(ok=False, body=payload, error="facilitator_unavailable", url=url)
    # Fail closed: HTTP 2xx required. 4xx/5xx must never look like a successful call.
    if not isinstance(status, int) or status < 200 or status >= 300:
        return FacilitatorResult(
            ok=False,
            body=payload or {},
            error="facilitator_http_%s" % status,
            url=url,
        )
    return FacilitatorResult(ok=True, body=payload, url=url)


def _request_body(payload: dict, accept: dict) -> dict:
    requirements = payment.official_requirements(accept)
    forwarded = payment.normalize_payload_for_facilitator(payload, requirements)
    return {
        "x402Version": 2,
        "paymentPayload": forwarded,
        "paymentRequirements": requirements,
    }


def verify(payload: dict, accept: dict, timeout: float | None = None) -> FacilitatorResult:
    rail = payment.rail_of_accept(accept)
    if rail is None:
        return FacilitatorResult(ok=False, error="unknown_payment_network")
    verify_url, _ = endpoints_for(rail)
    cap = VERIFY_TIMEOUT if timeout is None else max(0.05, float(timeout))
    result = _call(rail, verify_url, _request_body(payload, accept), cap)
    if not result.ok:
        return result
    body = result.body
    if body.get("isValid") is True:
        return FacilitatorResult(ok=True, body=body, url=verify_url)
    reason = (
        body.get("invalidReason")
        or body.get("invalidMessage")
        or body.get("error")
        or body.get("errorMessage")
        or "payment_invalid"
    )
    return FacilitatorResult(ok=False, body=body, error=str(reason), url=verify_url)


def settle(payload: dict, accept: dict, timeout: float | None = None) -> FacilitatorResult:
    rail = payment.rail_of_accept(accept)
    if rail is None:
        return FacilitatorResult(ok=False, error="unknown_payment_network")
    _, settle_url = endpoints_for(rail)
    cap = SETTLE_TIMEOUT if timeout is None else max(0.05, float(timeout))
    result = _call(rail, settle_url, _request_body(payload, accept), cap)
    if not result.ok:
        return result
    body = result.body
    if body.get("success") is True:
        return FacilitatorResult(ok=True, body=body, url=settle_url)
    reason = (
        body.get("errorReason")
        or body.get("errorMessage")
        or body.get("error")
        or "settlement_failed"
    )
    return FacilitatorResult(ok=False, body=body, error=str(reason), url=settle_url)
