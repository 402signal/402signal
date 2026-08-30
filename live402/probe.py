"""Probe a URL. Never pays upstream. Never holds keys."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from live402 import fixtures, select

USER_AGENT = "402Signal/0.1 (fail-closed probe; no payment)"
DISCOVERY_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
CATALOGS = (
    ("base", "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"),
    ("solana", "https://facilitator.payai.network/discovery/resources"),
    ("algorand", "https://facilitator.goplausible.xyz/discovery/resources"),
)
CATALOG_HOSTS = frozenset(
    (urlparse(url).hostname or "").lower() for _, url in CATALOGS
)
CATALOG_READ_LIMIT = 1_048_576
DEFAULT_TIMEOUT = 4.0
MAX_SINGLE_TIMEOUT = 10.0
DNS_TIMEOUT = 2.0
PROBE_BUDGET_SECONDS = 55.0
MAX_PROBE = 5
READ_LIMIT = 65536
MAX_REDIRECTS = 2
MISS_REASONS = (
    "no_candidates",
    "no_402_envelope",
    "no_payto",
    "reachable_200",
    "probe_timeout",
    "quote_expired",
    "invalid_need",
    "upstream_5xx",
    "ssrf",
    "no_input_schema",
    "constraints_unmet",
)
_MISS_MAP = {
    "empty_402": "no_402_envelope",
    "no_accepts": "no_402_envelope",
    "no_payto": "no_payto",
    "missing_payto": "no_payto",
    "http_200_no_challenge": "reachable_200",
    "timeout": "probe_timeout",
    "no_match": "no_candidates",
    "discovery_unavailable": "no_candidates",
    "get_405_post_failed": "no_402_envelope",
    "http_400": "no_402_envelope",
    "http_404": "no_402_envelope",
    "http_405": "no_402_envelope",
    "http_501": "no_402_envelope",
    "ssrf": "ssrf",
    "quote_expired": "quote_expired",
    "invalid_need": "invalid_need",
    "no_input_schema": "no_input_schema",
    "no_candidates": "no_candidates",
    "no_402_envelope": "no_402_envelope",
    "reachable_200": "reachable_200",
    "probe_timeout": "probe_timeout",
    "upstream_5xx": "upstream_5xx",
    "constraints_unmet": "constraints_unmet",
}
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.internal",
    "instance-data",
}
STOP = {
    "a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "via", "any",
    "with", "from", "by", "at", "is", "it", "as", "be", "this", "that", "api",
    "http", "https", "www", "com", "get", "post",
}


def probe_timeout() -> float:
    try:
        t = float(os.environ.get("LIVE402_PROBE_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        t = DEFAULT_TIMEOUT
    return min(max(t, 0.1), MAX_SINGLE_TIMEOUT)


def public_miss_reason(raw: str | None) -> str | None:
    """Map internal/legacy miss codes onto the public typed enum."""
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return None
    if key in _MISS_MAP:
        return _MISS_MAP[key]
    low = key.lower()
    if "expired" in low:
        return "quote_expired"
    if "ssrf" in low:
        return "ssrf"
    if "timeout" in low:
        return "probe_timeout"
    if key.startswith("http_"):
        try:
            code = int(key.split("_", 1)[1])
        except ValueError:
            return "no_402_envelope"
        if code == 200:
            return "reachable_200"
        if 500 <= code <= 599:
            return "upstream_5xx"
        return "no_402_envelope"
    if key in MISS_REASONS:
        return key
    return "no_402_envelope"


def remaining_timeout(deadline: float | None) -> float | None:
    """Seconds left before the <60s probe budget. None if no deadline."""
    if deadline is None:
        return None
    left = float(deadline) - time.monotonic()
    return left


def _request_timeout(deadline: float | None) -> float:
    cap = probe_timeout()
    left = remaining_timeout(deadline)
    if left is None:
        return cap
    if left <= 0.05:
        return 0.05
    return min(cap, left)


def _display_amount(amount, extra: dict | None) -> str | None:
    extra = extra if isinstance(extra, dict) else {}
    display = extra.get("displayAmount")
    if display:
        return str(display)
    if amount is None or amount == "":
        return None
    raw = str(amount).strip()
    if raw.startswith("$"):
        return raw
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return f"${n / 1_000_000:.2f}"


def _bazaar_blobs(item: dict | None, envelope: dict | None) -> list[dict]:
    out: list[dict] = []
    for blob in (envelope, item):
        if not isinstance(blob, dict):
            continue
        bazaar = (blob.get("extensions") or {}).get("bazaar")
        if isinstance(bazaar, dict):
            out.append(bazaar)
    return out


def extract_input_schema_source(item: dict | None, envelope: dict | None = None) -> tuple[dict | None, str | None]:
    """Return (schema, source). source is envelope, catalog, or bazaar."""
    if isinstance(envelope, dict) and isinstance(envelope.get("inputSchema"), dict) and envelope["inputSchema"]:
        schema = envelope["inputSchema"]
        if schema.get("properties") or schema.get("required") or schema.get("type"):
            return schema, "envelope"
    if isinstance(item, dict) and isinstance(item.get("inputSchema"), dict) and item["inputSchema"]:
        schema = item["inputSchema"]
        if schema.get("properties") or schema.get("required") or schema.get("type"):
            return schema, "catalog"
    for bazaar in _bazaar_blobs(item, envelope):
        info = bazaar.get("info") or {}
        inp = info.get("input") or {}
        if isinstance(inp, dict) and isinstance(inp.get("inputSchema"), dict) and inp["inputSchema"]:
            return inp["inputSchema"], "bazaar"
        schema = bazaar.get("schema") or {}
        props = (schema.get("properties") or {}).get("input") if isinstance(schema, dict) else None
        if not isinstance(props, dict):
            continue
        inner = props.get("properties") if isinstance(props.get("properties"), dict) else {}
        for key in ("body", "queryParams", "inputSchema"):
            cand = inner.get(key) if inner else props.get(key)
            if isinstance(cand, dict) and (cand.get("properties") or cand.get("required")):
                return cand, "bazaar"
        if props.get("properties") or props.get("required"):
            if props.get("type") == "object" or props.get("properties"):
                # Avoid returning the whole input descriptor (type/method) as a body schema.
                if "body" in inner or "queryParams" in inner or "method" in inner:
                    continue
                return props, "bazaar"
    return None, None


def extract_input_schema(item: dict | None, envelope: dict | None = None) -> dict | None:
    schema, _source = extract_input_schema_source(item, envelope)
    return schema


def extract_output_schema(item: dict | None, envelope: dict | None = None) -> dict | None:
    for blob in (envelope, item):
        if isinstance(blob, dict) and isinstance(blob.get("outputSchema"), dict) and blob["outputSchema"]:
            return blob["outputSchema"]
    for bazaar in _bazaar_blobs(item, envelope):
        info = bazaar.get("info") or {}
        out = info.get("output") or {}
        if isinstance(out, dict) and isinstance(out.get("schema"), dict) and out["schema"]:
            return out["schema"]
        schema = bazaar.get("schema") or {}
        props = (schema.get("properties") or {}).get("output") if isinstance(schema, dict) else None
        if isinstance(props, dict) and (props.get("properties") or props.get("type")):
            return props
    return None


def extract_method(item: dict | None, envelope: dict | None = None) -> str:
    for bazaar in _bazaar_blobs(item, envelope):
        info = bazaar.get("info") or {}
        inp = info.get("input") or {}
        if isinstance(inp, dict):
            method = str(inp.get("method") or "").strip().upper()
            if method:
                return method
            if str(inp.get("type") or "").lower() == "mcp":
                return "POST"
    return "POST"


def _facilitator_object(acc: dict) -> dict:
    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
    raw = extra.get("facilitator")
    url = None
    fee_payer = extra.get("feePayer")
    caip2 = extra.get("caip2")
    scheme = acc.get("scheme") or extra.get("scheme") or "exact"
    if isinstance(raw, str) and raw.strip().startswith("https://"):
        url = raw.strip()
    elif isinstance(raw, dict):
        cand = str(raw.get("url") or "").strip()
        if cand.startswith("https://"):
            url = cand
        fee_payer = raw.get("feePayer") or fee_payer
        caip2 = raw.get("caip2") or caip2
        scheme = raw.get("scheme") or scheme
    network = str(acc.get("network") or "")
    if not caip2 and ":" in network:
        caip2 = network
    obj = {}
    if url:
        obj["url"] = url
    if fee_payer:
        obj["feePayer"] = fee_payer
    if caip2:
        obj["caip2"] = caip2
    if scheme:
        obj["scheme"] = scheme
    return obj


def normalize_target_accepts(accepts: list | None) -> list[dict]:
    """Copy facilitator URL/feePayer/caip2/scheme onto each accept. Never invent x402.org."""
    out: list[dict] = []
    for acc in accepts or []:
        if not isinstance(acc, dict):
            continue
        row = dict(acc)
        extra = dict(row.get("extra") or {}) if isinstance(row.get("extra"), dict) else {}
        fac = _facilitator_object(row)
        if fac:
            extra["facilitator"] = fac
            if fac.get("feePayer") and not extra.get("feePayer"):
                extra["feePayer"] = fac["feePayer"]
            if fac.get("caip2") and not extra.get("caip2"):
                extra["caip2"] = fac["caip2"]
        row["extra"] = extra
        out.append(row)
    return out


def _accepts_from(item: dict | None, envelope: dict | None) -> list[dict]:
    for blob in (envelope, item):
        if isinstance(blob, dict):
            raw = blob.get("accepts")
            if isinstance(raw, list) and raw:
                return [a for a in raw if isinstance(a, dict)]
    return []


def build_target(item: dict | None, envelope: dict | None = None) -> dict:
    accepts = normalize_target_accepts(_accepts_from(item, envelope))
    first = accepts[0] if accepts else {}
    extra = first.get("extra") if isinstance(first.get("extra"), dict) else {}
    fac = extra.get("facilitator") if isinstance(extra.get("facilitator"), dict) else {}
    amount = first.get("amount") or first.get("maxAmountRequired")
    timeout = first.get("maxTimeoutSeconds")
    try:
        timeout_s = int(timeout) if timeout is not None else 60
    except (TypeError, ValueError):
        timeout_s = 60
    fac_url = fac.get("url") if isinstance(fac, dict) else None
    return {
        "method": extract_method(item, envelope),
        "inputSchema": extract_input_schema(item, envelope),
        "outputSchema": extract_output_schema(item, envelope),
        "accepts": accepts,
        "facilitator": fac_url,
        "amountAtomic": str(amount) if amount is not None else None,
        "displayAmount": _display_amount(amount, extra),
        "timeoutSeconds": timeout_s,
    }


def attach_invocable_target(result: dict, item: dict | None = None, envelope: dict | None = None) -> dict:
    """On a live probe, attach the invocable contract. Missing schema is not a fake miss of liveness."""
    env = envelope if isinstance(envelope, dict) else result.get("envelope")
    target = build_target(item, env)
    result["target"] = target
    schema, source = extract_input_schema_source(item, env)
    has_schema = isinstance(schema, dict) and bool(schema.get("properties") or schema.get("required"))
    live = bool(result.get("live"))
    result["invocable"] = bool(live and has_schema)
    if result["invocable"] and source:
        result["schema_source"] = source
        target["schema_source"] = source
    if live and not result["invocable"]:
        result["miss_reason"] = "no_input_schema"
    elif not live:
        result["invocable"] = False
        if result.get("miss_reason"):
            result["miss_reason"] = public_miss_reason(result.get("miss_reason"))
        # Keep target so the caller still sees catalog accepts/prices, but
        # a 503 is not an invocable handoff.
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProbeBlocked(Exception):
    """SSRF fail-closed. Must not be treated as a live upstream HTTP response."""


def _hostname(parsed) -> str:
    host = parsed.hostname
    if host:
        return host.strip().rstrip(".").lower()
    netloc = (parsed.netloc or "").split("@")[-1]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end > 0:
            return netloc[1:end].lower()
        return ""
    return netloc.split(":")[0].strip().rstrip(".").lower()


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return True
    return False


def _host_name_blocked(host: str) -> bool:
    if not host:
        return True
    if host in BLOCKED_HOSTS:
        return True
    if host.endswith((".localhost", ".local", ".internal", ".localdomain")):
        return True
    return False


def _try_ip(host: str):
    raw = (host or "").strip()
    if not raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _getaddrinfo_timed(host: str, timeout: float | None = None):
    """socket.getaddrinfo with a join timeout. Fail closed on hang."""
    cap = DNS_TIMEOUT if timeout is None else float(timeout)
    if cap <= 0:
        raise TimeoutError("getaddrinfo timed out")
    box: list = []

    def run() -> None:
        try:
            box.append(
                ("ok", socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
            )
        except Exception as exc:
            box.append(("err", exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(cap)
    if not box:
        raise TimeoutError("getaddrinfo timed out")
    kind, payload = box[0]
    if kind == "err":
        raise payload
    return payload


def _resolve_public(host: str) -> bool:
    """DNS-resolve host and reject unless every address is a public IP. Fail closed."""
    literal = _try_ip(host)
    if literal is not None:
        return not _ip_blocked(literal)
    if _host_name_blocked(host):
        return False
    try:
        infos = _getaddrinfo_timed(host)
    except (OSError, TimeoutError):
        return False
    if not infos:
        return False
    seen = False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if _ip_blocked(ip):
            return False
        seen = True
    return seen


def catalog_url_allowed(url: str) -> bool:
    """HTTPS + hardcoded catalog host only. Used by /pulse and discovery."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    host = _hostname(parsed)
    return bool(host) and host in CATALOG_HOSTS



def pulse_catalogs() -> tuple[tuple[str, str], ...]:
    """Same allowlisted hosts as CATALOGS. Pagination lives in catalog.py."""
    return CATALOGS


def safe_target(url: str) -> str | None:
    """Return the URL if it is https to a public host, else None. Fail closed."""
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    host = _hostname(parsed)
    if not host or _host_name_blocked(host):
        return None
    if not _resolve_public(host):
        return None
    return raw


def _https_url(url: str) -> str | None:
    """Scheme/userinfo gate used when ranking catalogs. Full SSRF is safe_target()."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    host = _hostname(parsed)
    if not host or _host_name_blocked(host):
        return None
    literal = _try_ip(host)
    if literal is not None and _ip_blocked(literal):
        return None
    return raw


def skip_candidate_url(url: str) -> bool:
    """Drop localhost and :param / {param} path templates from samples and probe candidates."""
    raw = (url or "").strip()
    if not raw:
        return True
    parsed = urlparse(raw)
    host = _hostname(parsed)
    if host in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if host in {"127.0.0.1", "::1", "0.0.0.0"} or host.startswith("127."):
        return True
    for part in (parsed.path or "").split("/"):
        if not part:
            continue
        if part.startswith(":") or part.startswith("{") or part.startswith("<"):
            return True
        if "{" in part or "}" in part:
            return True
    return False


PREFER_NETWORKS = ("base", "solana", "algorand")


def normalize_prefer_network(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val in PREFER_NETWORKS:
        return val
    return None


def _settlement_score(item: dict | None) -> int:
    """Numeric catalog traction used to rank live hits. Unknown -> 0."""
    raw = _traction(item)
    if not raw or raw == "unknown":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "ssrf_hops", 0) + 1
        if hops > MAX_REDIRECTS:
            raise ProbeBlocked("too many redirects")
        joined = urljoin(req.full_url, newurl)
        if not safe_target(joined):
            raise ProbeBlocked("blocked redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is None:
            raise ProbeBlocked("blocked redirect")
        nxt.ssrf_hops = hops
        return nxt


def _opener():
    return urllib.request.build_opener(_SSRFRedirectHandler)


def _has_402_challenge(status: int | None, headers: dict[str, str]) -> bool:
    if status == 402:
        return True
    for key, val in headers.items():
        k = key.lower()
        if k in {"payment-required", "x-payment-required", "payment-challenges"}:
            return True
        if k == "www-authenticate" and "402" in (val or "").lower():
            return True
    return False


def _headers_map(hdrs) -> dict[str, str]:
    if not hdrs:
        return {}
    return {str(k).lower(): str(v) for k, v in hdrs.items()}


def _decode_envelope_blob(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    try:
        padded = text + ("=" * ((4 - len(text) % 4) % 4))
        decoded = base64.b64decode(padded, validate=False)
        candidates.insert(0, decoded.decode("utf-8"))
    except Exception:
        pass
    for item in candidates:
        try:
            payload = json.loads(item)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _envelope_is_parseable(env: dict | None) -> bool:
    if not env or not isinstance(env, dict):
        return False
    accepts = env.get("accepts")
    has_accepts = isinstance(accepts, list) and len(accepts) > 0
    has_version = env.get("x402Version") is not None
    return has_accepts and has_version


def _payto_from_envelope(env: dict | None) -> str | None:
    """First non-empty accepts[].payTo. Empty 402s have no usable payTo."""
    if not env or not isinstance(env, dict):
        return None
    accepts = env.get("accepts") or []
    if not isinstance(accepts, list):
        return None
    for acc in accepts:
        if not isinstance(acc, dict):
            continue
        val = acc.get("payTo")
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def parse_envelope(status: int | None, headers: dict[str, str], body: bytes) -> tuple[dict | None, str | None]:
    """Parse a payment envelope. Live only on HTTP 402 with parseable accepts/x402Version."""
    headers = headers or {}
    header_env = None
    for key in ("payment-required", "x-payment-required"):
        val = headers.get(key)
        if val:
            header_env = _decode_envelope_blob(val)
            if header_env:
                break

    body_env = None
    raw = body or b""
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                body_env = parsed
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            body_env = None

    if status != 402:
        if status == 200:
            return None, "reachable_200"
        if status is None:
            return None, "probe_timeout"
        if 500 <= int(status) <= 599:
            return None, "upstream_5xx"
        return None, "no_402_envelope"

    envelope = header_env if _envelope_is_parseable(header_env) else None
    if envelope is None and body_env is not None:
        if _envelope_is_parseable(body_env):
            envelope = body_env

    if envelope is not None:
        if not _payto_from_envelope(envelope):
            return None, "no_payto"
        return envelope, None

    return None, "no_402_envelope"


def _read_limited(fp) -> bytes:
    try:
        return fp.read(READ_LIMIT) if fp is not None else b""
    except Exception:
        return b""


def _miss_from_status(status: int | None) -> str:
    if status == 200:
        return "reachable_200"
    if status is None:
        return "probe_timeout"
    if isinstance(status, int) and 500 <= status <= 599:
        return "upstream_5xx"
    return "no_402_envelope"


def _declared_input_body(item: dict | None) -> dict | None:
    if not item or not isinstance(item, dict):
        return None
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    if not isinstance(bazaar, dict):
        return None
    info = bazaar.get("info") or {}
    if not isinstance(info, dict):
        return None
    inp = info.get("input") or {}
    if not isinstance(inp, dict):
        return None
    body = inp.get("body")
    if isinstance(body, dict) and body:
        return body
    return None


def _catalog_payto(item: dict | None) -> str | None:
    if not item or not isinstance(item, dict):
        return None
    accepts = item.get("accepts") or []
    if not isinstance(accepts, list):
        return None
    for acc in accepts:
        if isinstance(acc, dict):
            val = acc.get("payTo")
            if val and str(val).strip():
                return str(val).strip()
    return None


def _traction(item: dict | None) -> str:
    """Numeric catalog traction only. Prefer unknown over a guessed volume."""
    if not item or not isinstance(item, dict):
        return "unknown"
    keys = (
        "x402Requests",
        "requestCount",
        "totalRequests",
        "requests",
        "qualityCalls",
        "calls",
        "settleCount",
    )
    blobs = [item]
    quality = item.get("quality")
    if isinstance(quality, dict):
        blobs.append(quality)
        val = quality.get("l30DaysTotalCalls")
        if not isinstance(val, bool) and isinstance(val, (int, float)) and val >= 0:
            return str(int(val))
    meta = item.get("metadata")
    if isinstance(meta, dict):
        blobs.append(meta)
        disc = meta.get("discovery")
        if isinstance(disc, dict):
            blobs.append(disc)
    info = item.get("discoveryInfo")
    if isinstance(info, dict):
        blobs.append(info)
    for blob in blobs:
        for key in keys:
            val = blob.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)) and val >= 0:
                return str(int(val))
    return "unknown"


def attach_catalog_fields(result: dict, item: dict | None = None) -> dict:
    result["traction"] = _traction(item)
    result.setdefault("payTo", None)
    catalog_pay = _catalog_payto(item)
    probed = result.get("payTo")
    if catalog_pay:
        mismatched = bool(
            probed and str(probed).strip().lower() != catalog_pay.lower()
        )
        if mismatched:
            result["payTo_changed"] = True
        else:
            result.setdefault("payTo_changed", False)
    return result


def _finalize_probe(result: dict) -> dict:
    """Record history and attach freshness/readiness. Never raises."""
    try:
        from live402 import history as history_mod
        meta = history_mod.record_probe(result.get("url") or "", result)
        return history_mod.attach_to_result(result, meta)
    except Exception:
        result.setdefault("verified_at", result.get("probed_at"))
        result.setdefault("verified_seconds_ago", 0)
        if result.get("payTo_changed"):
            result.setdefault("risk", ["payTo_changed"])
        result.setdefault("readiness", "discovered")
        result.setdefault(
            "history",
            {
                "success_24h": None,
                "success_7d": None,
                "n_24h": 0,
                "n_7d": 0,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
            },
        )
        return result


def health_from_probe(url: str, snap: dict) -> dict:
    probed_at = snap.get("probed_at") or now_iso()
    live = bool(snap.get("live"))
    out = {
        "live": live,
        "url": url,
        "status": snap.get("status"),
        "latency_ms": snap.get("latency_ms"),
        "has_402_challenge": bool(snap.get("has_402_challenge")),
        "probed_at": probed_at,
        "payTo": snap.get("payTo"),
        "health": {
            "live": live,
            "last_probe": probed_at,
            "latency_ms": snap.get("latency_ms"),
            "has_402_challenge": bool(snap.get("has_402_challenge")),
            "status": snap.get("status"),
        },
    }
    if snap.get("probes") is not None:
        out["probes"] = snap["probes"]
    if not live and snap.get("miss_reason"):
        out["miss_reason"] = public_miss_reason(snap["miss_reason"]) or snap["miss_reason"]
    if snap.get("traction") is not None:
        out["traction"] = snap["traction"]
    if "payTo_changed" in snap:
        out["payTo_changed"] = snap["payTo_changed"]
    return out


def _probe_entry(method: str, snap: dict) -> dict:
    entry = {"method": method, "status": snap.get("status")}
    if not snap.get("live") and snap.get("miss_reason"):
        entry["miss_reason"] = public_miss_reason(snap["miss_reason"]) or snap["miss_reason"]
    return entry


def _one_request(url: str, method: str, data: bytes | None = None, deadline: float | None = None) -> dict:
    """Single unpaid HTTP probe. Never pays. ProbeBlocked is ssrf, never live."""
    if remaining_timeout(deadline) is not None and remaining_timeout(deadline) <= 0:
        return {
            "live": False,
            "status": None,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": "probe_timeout",
            "envelope": None,
        }
    timeout = _request_timeout(deadline)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    req.ssrf_hops = 0
    opener = _opener()
    status = None
    hdrs: dict[str, str] = {}
    body = b""
    try:
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                hdrs = _headers_map(resp.headers)
                body = _read_limited(resp)
        except ProbeBlocked:
            raise
        except urllib.error.HTTPError as err:
            status = err.code
            hdrs = _headers_map(err.headers)
            body = _read_limited(err)
    except ProbeBlocked:
        return {
            "live": False,
            "status": None,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": "ssrf",
            "envelope": None,
        }
    except Exception as exc:
        reason = "no_402_envelope"
        name = type(exc).__name__.lower()
        msg = str(getattr(exc, "reason", exc) or "").lower()
        if "timed out" in msg or "timeout" in name or "timeout" in msg:
            reason = "probe_timeout"
        elif isinstance(exc, socket.timeout):
            reason = "probe_timeout"
        return {
            "live": False,
            "status": None,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": reason,
            "envelope": None,
        }

    envelope, miss = parse_envelope(status, hdrs, body)
    live = envelope is not None and miss is None and status == 402
    return {
        "live": live,
        "status": status,
        "has_402_challenge": _has_402_challenge(status, hdrs),
        "payTo": _payto_from_envelope(envelope) if live else None,
        "miss_reason": None if live else (miss or _miss_from_status(status)),
        "envelope": envelope if live else None,
    }


def _infer_fixture_miss(canned: dict) -> str:
    if canned.get("miss_reason"):
        return public_miss_reason(str(canned["miss_reason"])) or "no_402_envelope"
    status = canned.get("status")
    return _miss_from_status(status)


def _fixture_probe(url: str, catalog_item: dict | None = None) -> dict:
    row = fixtures.lookup_url(url)
    probed_at = now_iso()
    if not row:
        return _finalize_probe(
            health_from_probe(
                url,
                {
                    "live": False,
                    "status": None,
                    "latency_ms": 0,
                    "has_402_challenge": False,
                    "payTo": None,
                    "miss_reason": "http_404",
                    "probes": [],
                    "probed_at": probed_at,
                },
            )
        )
    canned = dict(row.get("probe") or {})
    canned["probed_at"] = probed_at
    payable = (
        bool(canned.get("live"))
        and canned.get("status") == 402
        and bool(canned.get("has_402_challenge"))
    )
    canned["live"] = payable
    canned.setdefault("payTo", None)
    if not payable:
        canned["miss_reason"] = _infer_fixture_miss(canned)
    if "probes" not in canned:
        entry = {"method": "GET", "status": canned.get("status")}
        if not payable and canned.get("miss_reason"):
            entry["miss_reason"] = canned["miss_reason"]
        canned["probes"] = [entry]
    result = health_from_probe(row.get("url") or url, canned)
    result = attach_catalog_fields(result, catalog_item or row)
    result = attach_invocable_target(result, catalog_item or row)
    return _finalize_probe(result)


def probe_url(url: str, catalog_item: dict | None = None, deadline: float | None = None) -> dict:
    """Unpaid dual probe. Live = HTTP 402 with a parseable payment envelope."""
    if deadline is None:
        deadline = time.monotonic() + PROBE_BUDGET_SECONDS
    if fixtures.fixture_mode():
        return _fixture_probe(url, catalog_item)

    if remaining_timeout(deadline) is not None and remaining_timeout(deadline) <= 0:
        result = health_from_probe(
            url,
            {
                "live": False,
                "status": None,
                "latency_ms": 0,
                "has_402_challenge": False,
                "payTo": None,
                "miss_reason": "probe_timeout",
                "probes": [],
                "probed_at": now_iso(),
            },
        )
        result = attach_catalog_fields(result, catalog_item)
        return _finalize_probe(attach_invocable_target(result, catalog_item))

    safe = safe_target(url)
    if not safe:
        result = health_from_probe(
            url,
            {
                "live": False,
                "status": None,
                "latency_ms": 0,
                "has_402_challenge": False,
                "payTo": None,
                "miss_reason": "ssrf",
                "probes": [],
                "probed_at": now_iso(),
            },
        )
        return _finalize_probe(attach_invocable_target(result, catalog_item))

    start = time.perf_counter()
    probes: list[dict] = []

    get_snap = _one_request(safe, "GET", deadline=deadline)
    probes.append(_probe_entry("GET", get_snap))
    if get_snap.get("miss_reason") == "ssrf":
        latency_ms = int((time.perf_counter() - start) * 1000)
        get_snap["latency_ms"] = latency_ms
        get_snap["probes"] = probes
        get_snap["probed_at"] = now_iso()
        result = health_from_probe(safe, get_snap)
        return _finalize_probe(attach_invocable_target(result, catalog_item))

    post_snap = None
    if not get_snap.get("live"):
        post_snap = _one_request(safe, "POST", data=b"{}", deadline=deadline)
        if get_snap.get("status") in {405, 501} and not post_snap.get("live"):
            post_snap["miss_reason"] = "no_402_envelope"
        probes.append(_probe_entry("POST", post_snap))

    declared_snap = None
    declared = _declared_input_body(catalog_item)
    if declared is not None:
        raw = json.dumps(declared, separators=(",", ":")).encode("utf-8")
        declared_snap = _one_request(safe, "POST", data=raw, deadline=deadline)
        probes.append(_probe_entry("POST", declared_snap))

    unpaid_live = bool(get_snap.get("live") or (post_snap and post_snap.get("live")))
    if declared_snap is not None:
        dstatus = declared_snap.get("status")
        dmiss = declared_snap.get("miss_reason")
        if dstatus == 200 or dmiss in {"no_402_envelope", "reachable_200", "empty_402", "http_200_no_challenge"}:
            unpaid_live = False

    winner = None
    if get_snap.get("live") and unpaid_live:
        winner = get_snap
    elif post_snap and post_snap.get("live") and unpaid_live:
        winner = post_snap
    elif declared_snap and declared_snap.get("live") and unpaid_live:
        winner = declared_snap
    else:
        winner = post_snap or get_snap
        if declared_snap and not unpaid_live:
            if declared_snap.get("status") == 200 or declared_snap.get("miss_reason") in {
                "empty_402",
                "http_200_no_challenge",
                "no_402_envelope",
                "reachable_200",
            }:
                winner = declared_snap

    live = bool(unpaid_live and winner and winner.get("live"))
    miss_reason = None
    if not live:
        if get_snap.get("status") in {405, 501} and not (post_snap and post_snap.get("live")):
            miss_reason = "no_402_envelope"
        else:
            miss_reason = public_miss_reason((winner or {}).get("miss_reason") or "probe_timeout")

    latency_ms = int((time.perf_counter() - start) * 1000)
    snap = {
        "live": live,
        "status": (winner or {}).get("status"),
        "latency_ms": latency_ms,
        "has_402_challenge": bool((winner or {}).get("has_402_challenge")),
        "payTo": (winner or {}).get("payTo") if live else (winner or {}).get("payTo"),
        "probes": probes,
        "probed_at": now_iso(),
    }
    if not live:
        snap["miss_reason"] = miss_reason
        snap["payTo"] = None if not live else snap.get("payTo")
    if winner and winner.get("envelope"):
        snap["envelope"] = winner.get("envelope")
    result = health_from_probe(safe, snap)
    if snap.get("envelope"):
        result["envelope"] = snap["envelope"]
    result = attach_catalog_fields(result, catalog_item)
    result = attach_invocable_target(result, catalog_item, snap.get("envelope"))
    return _finalize_probe(result)


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    buf = []
    for ch in (text or "").lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tok = "".join(buf)
                if len(tok) >= 3 and tok not in STOP:
                    out.add(tok)
                buf = []
    if buf:
        tok = "".join(buf)
        if len(tok) >= 3 and tok not in STOP:
            out.add(tok)
    return out


def _resource_url(item: dict) -> str:
    raw = (
        item.get("resource")
        or item.get("resourceUrl")
        or item.get("url")
        or ""
    )
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("resourceUrl") or ""
    return str(raw).strip()


def _item_rail(item: dict) -> str:
    tagged = item.get("_rail")
    if tagged:
        return str(tagged)
    accepts = item.get("accepts") or []
    nets = []
    for acc in accepts:
        if isinstance(acc, dict):
            nets.append(str(acc.get("network") or ""))
    blob = " ".join(nets).lower()
    if "algorand" in blob:
        return "algorand"
    if "solana" in blob:
        return "solana"
    if "8453" in blob or "base" in blob:
        return "base"
    return "unknown"


def _resource_blob(item: dict) -> str:
    parts = [
        _resource_url(item),
        item.get("description") or "",
        item.get("serviceName") or "",
        " ".join(item.get("tags") or []),
    ]
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    info = bazaar.get("info") or {}
    inp = info.get("input") or {}
    parts.append(str(inp.get("toolName") or ""))
    return " ".join(str(p) for p in parts)


def score_need(need: str, item: dict) -> int:
    q = _tokens(need)
    if not q:
        return 0
    score = 0
    try:
        from live402 import catalog as catalog_mod
        need_cap = catalog_mod.capability_for_need(need)
        item_cap = item.get("capability")
        if not item_cap or item_cap == "unknown":
            item_cap, _src = catalog_mod.classify_capability(item)
    except Exception:
        need_cap = "unknown"
        item_cap = item.get("capability") or "unknown"
    if need_cap and need_cap != "unknown" and item_cap == need_cap:
        score += 100
    blob = _resource_blob(item)
    hay = _tokens(blob)
    hit = q & hay
    score += len(hit) * 10
    low = blob.lower()
    for tok in q:
        if tok in low:
            score += 2
    if score <= 0:
        return 0
    if item.get("_input_schema_present"):
        score += 8
    if item.get("_output_schema_present"):
        score += 4
    return score


def rank_resources(need: str, items: list[dict], prefer_network: str | None = None) -> list[dict]:
    prefer = normalize_prefer_network(prefer_network)
    ranked = []
    for item in items:
        url = _resource_url(item)
        if skip_candidate_url(url):
            continue
        if not _https_url(url) and not fixtures.fixture_mode():
            continue
        if fixtures.fixture_mode() and not url:
            continue
        s = score_need(need, item)
        if s <= 0:
            continue
        rail = _item_rail(item)
        prefer_hit = 1 if prefer and rail == prefer else 0
        ranked.append((prefer_hit, s, _settlement_score(item), item))
    ranked.sort(key=lambda pair: (pair[0], pair[1], pair[2]), reverse=True)
    return [item for _, _, _, item in ranked]


class _CatalogRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "ssrf_hops", 0) + 1
        if hops > 2:
            raise ProbeBlocked("too many catalog redirects")
        joined = urljoin(req.full_url, newurl)
        if not catalog_url_allowed(joined):
            raise ProbeBlocked("catalog redirect not allowlisted")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is None:
            raise ProbeBlocked("catalog redirect blocked")
        nxt.ssrf_hops = hops
        return nxt


def _catalog_opener():
    return urllib.request.build_opener(_CatalogRedirectHandler)


def _fetch_catalog_payload(url: str, timeout: float, read_limit: int | None = None):
    """Fetch allowlisted catalog JSON. Empty dict if blocked or oversize."""
    if not catalog_url_allowed(url):
        return {}
    cap = CATALOG_READ_LIMIT if read_limit is None else int(read_limit)
    if cap < 1:
        return {}
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    req.ssrf_hops = 0
    opener = _catalog_opener()
    with opener.open(req, timeout=timeout) as resp:
        final = ""
        getter = getattr(resp, "geturl", None)
        if callable(getter):
            final = getter() or ""
        if final and not catalog_url_allowed(final):
            return {}
        raw = resp.read(cap + 1)
    if len(raw) > cap:
        return {}
    payload = json.loads(raw.decode("utf-8"))
    return payload


def _fetch_one_catalog(rail: str, url: str, timeout: float) -> list[dict]:
    payload = _fetch_catalog_payload(url, timeout)
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = list(payload.get("items") or payload.get("resources") or [])
    else:
        items = []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["_rail"] = rail
        out.append(row)
    return out


def fetch_discovery(limit: int = 20) -> list[dict]:
    if fixtures.fixture_mode():
        rows = fixtures.load_resources()
        for row in rows:
            row.setdefault("_rail", "fixture")
        return rows
    from live402 import catalog as catalog_mod
    # limit kept for compat; do not truncate the paginated index.
    return list(catalog_mod.get_index().get("items") or [])


def _discovery_unavailable_miss(objective: str) -> dict:
    probed_at = now_iso()
    return {
        "live": False,
        "invocable": False,
        "url": None,
        "tried": 0,
        "error": "discovery_unavailable",
        "payTo": None,
        "traction": "unknown",
        "miss_reason": "no_candidates",
        "target": None,
        "probes": [],
        "probed_at": probed_at,
        "objective": objective,
        "compared": [],
        "health": {
            "live": False,
            "last_probe": probed_at,
            "latency_ms": None,
            "has_402_challenge": False,
            "status": None,
        },
    }


def _attach_selection(body: dict, probed: list, winner, objective: str) -> dict:
    body["objective"] = objective
    body["compared"] = select.comparison(probed, winner)
    body["tried"] = len(probed)
    return body


def _selection_set(probed: list) -> list:
    """Live hits. Drop payTo_changed when a stable live hit exists in the window."""
    live_hits = [r for r in probed if isinstance(r, dict) and r.get("live")]
    if any(not r.get("payTo_changed") for r in live_hits):
        return [r for r in live_hits if not r.get("payTo_changed")]
    return live_hits


def route_need(
    need: str,
    deadline: float | None = None,
    prefer_network: str | None = None,
    objective: str | None = None,
    constraints: dict | None = None,
) -> dict:
    """Fuzzy-match discovery, probe up to 5, pick best-of-N. Else fail-closed."""
    if deadline is None:
        deadline = time.monotonic() + PROBE_BUDGET_SECONDS
    prefer = normalize_prefer_network(prefer_network)
    obj = select.parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    try:
        items = fetch_discovery()
    except Exception:
        return _discovery_unavailable_miss(obj)
    ranked = rank_resources(need, items, prefer_network=prefer)
    probed: list[dict] = []
    last = None
    for item in ranked[:MAX_PROBE]:
        if remaining_timeout(deadline) is not None and remaining_timeout(deadline) <= 0:
            if not probed:
                last = {
                    "live": False,
                    "invocable": False,
                    "url": _resource_url(item),
                    "status": None,
                    "latency_ms": 0,
                    "has_402_challenge": False,
                    "payTo": None,
                    "miss_reason": "probe_timeout",
                    "probes": [],
                    "probed_at": now_iso(),
                }
            break
        url = _resource_url(item)
        last = probe_url(url, catalog_item=item, deadline=deadline)
        last = attach_catalog_fields(last, item)
        try:
            from live402 import history as history_mod
            last = history_mod.attach_to_result(last)
        except Exception:
            if last.get("payTo_changed"):
                last["risk"] = ["payTo_changed"]
        last["need"] = need
        last["rail"] = _item_rail(item)
        last["source"] = "fixture" if fixtures.fixture_mode() else "discovery"
        probed.append(last)
    selection_set = _selection_set(probed)
    winner = select.pick_winner(selection_set, obj, cons)
    if winner:
        return _attach_selection(winner, probed, winner, obj)
    some_live = any(isinstance(r, dict) and r.get("live") for r in probed)
    body = {
        "live": False,
        "invocable": False,
        "url": None,
        "tried": len(probed),
        "need": need,
        "source": "fixture" if fixtures.fixture_mode() else "discovery",
        "payTo": None,
        "traction": (last or {}).get("traction") or "unknown",
        "probes": (last or {}).get("probes") or [],
        "status": (last or {}).get("status"),
        "latency_ms": (last or {}).get("latency_ms"),
        "has_402_challenge": bool((last or {}).get("has_402_challenge")),
        "target": None,
        "probed_at": now_iso(),
        "health": {
            "live": False,
            "last_probe": (last or {}).get("probed_at") or now_iso(),
            "latency_ms": (last or {}).get("latency_ms"),
            "has_402_challenge": bool((last or {}).get("has_402_challenge")),
            "status": (last or {}).get("status"),
        },
    }
    if some_live:
        body["miss_reason"] = "constraints_unmet"
    elif last and last.get("miss_reason"):
        body["miss_reason"] = public_miss_reason(last.get("miss_reason")) or last.get("miss_reason")
    elif not probed:
        body["miss_reason"] = "no_candidates"
    if last:
        body["last"] = {
            "url": last.get("url"),
            "status": last.get("status"),
            "latency_ms": last.get("latency_ms"),
            "miss_reason": last.get("miss_reason"),
        }
        if last.get("rail"):
            body["rail"] = last.get("rail")
    return _attach_selection(body, probed, None, obj)
