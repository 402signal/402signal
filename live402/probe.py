"""Probe a URL. Never pays upstream. Never holds keys."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeout, wait
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from live402 import clock, fixtures, payment, select

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
# Hard server ceiling. Do not treat this as the typical per-request budget.
PROBE_CEILING = 20
FIRST_TRANCHE = 3
EXPAND_TRANCHE = 3
STANDARD_PROBE_CAP = 7
THOROUGH_PROBE_CAP = 15
MAX_IN_FLIGHT = 3
MAX_PROCESS_PROBES = 10
MAX_PER_HOST = 2
MAX_HOST_SLOT_KEYS = 256
MAX_DNS_WORKERS = 8
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
    "probe_budget_exhausted",
    "probe_limit_reached",
    "unsafe_to_probe",
)
STOP_REASONS = (
    "winner_selected",
    "candidate_set_exhausted",
    "probe_limit_reached",
    "probe_budget_exhausted",
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
    "probe_budget_exhausted": "probe_budget_exhausted",
    "probe_limit_reached": "probe_limit_reached",
    "unsafe_to_probe": "unsafe_to_probe",
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
    left = float(deadline) - clock.monotonic()
    return left


def parse_search_depth(raw) -> str:
    if raw is None:
        return "standard"
    text = str(raw).strip().lower()
    if text == "thorough":
        return "thorough"
    return "standard"


def probe_plan(
    body=None,
    search_depth=None,
    max_candidates_to_probe=None,
) -> dict:
    """Per-request probe budget. Typical is 3 then +2–4. Hard cap is 20."""
    src = body if isinstance(body, dict) else {}
    depth = parse_search_depth(
        search_depth if search_depth is not None else src.get("search_depth")
    )
    raw_max = max_candidates_to_probe
    if raw_max is None:
        raw_max = src.get("max_candidates_to_probe")
    requested = select._nonneg_int(raw_max)
    if requested is not None and requested < 1:
        requested = None
    if requested is not None:
        ceiling = min(int(requested), PROBE_CEILING)
    elif depth == "thorough":
        ceiling = min(THOROUGH_PROBE_CAP, PROBE_CEILING)
    else:
        ceiling = min(STANDARD_PROBE_CAP, PROBE_CEILING)
    if ceiling < 1:
        ceiling = 1
    return {
        "search_depth": depth,
        "max_candidates_to_probe": requested,
        "probe_ceiling": ceiling,
    }


def next_tranche_size(probed_n: int, remaining: int, ceiling: int, first: bool) -> int:
    """First tranche is 3. Later expansions are 2–4. Never past the request ceiling."""
    left = min(int(remaining), int(ceiling) - int(probed_n))
    if left <= 0:
        return 0
    if first:
        return min(FIRST_TRANCHE, left)
    return min(EXPAND_TRANCHE, left)


_pool_lock = threading.Lock()
_shared_pool: ThreadPoolExecutor | None = None
_global_slots = threading.Semaphore(MAX_PROCESS_PROBES)
_host_slots: OrderedDict[str, threading.Semaphore] = OrderedDict()
_host_slots_lock = threading.Lock()
_overflow_host_sem = threading.Semaphore(1)
_inflight_lock = threading.Lock()
_inflight = 0
_inflight_peak = 0
_dns_lock = threading.Lock()
_dns_pool: ThreadPoolExecutor | None = None


def _shared_probe_pool() -> ThreadPoolExecutor:
    global _shared_pool
    with _pool_lock:
        if _shared_pool is None:
            _shared_pool = ThreadPoolExecutor(
                max_workers=MAX_PROCESS_PROBES,
                thread_name_prefix="probe",
            )
        return _shared_pool


def _probe_host(url: str | None) -> str | None:
    try:
        host = (urlparse(url or "").hostname or "").strip().lower()
    except Exception:
        return None
    return host or None


def _host_slot_released(sem: threading.Semaphore) -> bool:
    value = getattr(sem, "_value", None)
    if isinstance(value, int):
        return value >= MAX_PER_HOST
    return False


def _host_semaphore(host: str | None) -> threading.Semaphore | None:
    if not host:
        return None
    with _host_slots_lock:
        sem = _host_slots.get(host)
        if sem is not None:
            _host_slots.move_to_end(host)
            return sem
        while len(_host_slots) >= MAX_HOST_SLOT_KEYS:
            evicted = False
            for key, old in list(_host_slots.items()):
                if _host_slot_released(old):
                    del _host_slots[key]
                    evicted = True
                    break
            if not evicted:
                return _overflow_host_sem
        sem = threading.Semaphore(MAX_PER_HOST)
        _host_slots[host] = sem
        return sem


def host_slot_cache_size() -> int:
    with _host_slots_lock:
        return len(_host_slots)


def process_probe_inflight() -> int:
    with _inflight_lock:
        return _inflight


def process_probe_inflight_peak() -> int:
    with _inflight_lock:
        return _inflight_peak


def reset_probe_inflight_peak() -> None:
    global _inflight_peak
    with _inflight_lock:
        _inflight_peak = _inflight


def acquire_probe_slot(host: str | None, deadline: float | None) -> bool:
    """Wait for a process-wide slot without holding a host lock. Fail closed.

    Per-host cap is try-acquire only while a global slot is already held; if the
    host is busy the global slot is released and we retry. Never nests blocking
    waits, so one slow merchant cannot deadlock unrelated probes.
    """
    global _inflight, _inflight_peak
    started = clock.monotonic()
    while True:
        left = remaining_timeout(deadline)
        if left is not None and left <= 0:
            return False
        if deadline is None and (clock.monotonic() - started) >= 5.0:
            return False
        if not _global_slots.acquire(blocking=False):
            wait = 0.02 if left is None else min(0.02, max(0.0, left))
            if wait <= 0:
                return False
            if not _global_slots.acquire(timeout=wait):
                continue
        host_sem = _host_semaphore(host)
        if host_sem is not None and not host_sem.acquire(blocking=False):
            _global_slots.release()
            left = remaining_timeout(deadline)
            if left is not None and left <= 0:
                return False
            time.sleep(0.01 if left is None else min(0.01, max(0.0, left)))
            continue
        with _inflight_lock:
            _inflight += 1
            if _inflight > _inflight_peak:
                _inflight_peak = _inflight
        return True


def release_probe_slot(host: str | None) -> None:
    global _inflight
    host_sem = _host_semaphore(host)
    if host_sem is not None:
        try:
            host_sem.release()
        except Exception:
            pass
    try:
        _global_slots.release()
    except Exception:
        pass
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


def _request_timeout(deadline: float | None) -> float:
    cap = probe_timeout()
    left = remaining_timeout(deadline)
    if left is None:
        return cap
    if left <= 0.05:
        return 0.05
    return min(cap, left)


def _display_amount(amount, extra: dict | None, asset=None, network=None) -> str | None:
    extra = extra if isinstance(extra, dict) else {}
    acc = {
        "amount": amount,
        "asset": asset,
        "network": network,
        "extra": extra,
    }
    opt = payment.payment_option_from_accept(acc, network)
    if opt and opt.get("display_amount"):
        return opt["display_amount"]
    seller = extra.get("displayAmount")
    if seller:
        return str(seller)
    if amount is None or amount == "":
        return None
    raw = str(amount).strip()
    if raw.startswith("$"):
        return raw
    return None


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
    """CURRENT HTTP 402 envelope accepts only. Catalog claims never enter target.accepts."""
    _ = item
    out: list[dict] = []
    if not isinstance(envelope, dict):
        return out
    raw = envelope.get("accepts")
    if not isinstance(raw, list):
        return out
    seen: set[tuple] = set()
    for acc in raw:
        if not isinstance(acc, dict):
            continue
        key = payment.accept_identity(acc)
        if key in seen:
            continue
        seen.add(key)
        out.append(acc)
    return out


def _catalog_accepts(item: dict | None) -> list[dict]:
    if not isinstance(item, dict):
        return []
    raw = item.get("accepts")
    if not isinstance(raw, list):
        return []
    return [acc for acc in raw if isinstance(acc, dict)]


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
    opt = payment.payment_option_from_accept(first, first.get("network"))
    display = (opt or {}).get("display_amount")
    if display is None:
        display = _display_amount(amount, extra, first.get("asset") or first.get("currency"), first.get("network"))
    return {
        "method": extract_method(item, envelope),
        "inputSchema": extract_input_schema(item, envelope),
        "outputSchema": extract_output_schema(item, envelope),
        "accepts": accepts,
        "facilitator": fac_url,
        "amountAtomic": str(amount) if amount is not None else None,
        "displayAmount": display,
        "timeoutSeconds": timeout_s,
    }


def attach_invocable_target(result: dict, item: dict | None = None, envelope: dict | None = None) -> dict:
    """On a live probe, attach the invocable contract. Missing schema is not a fake miss of liveness.

    challenge_observed = HTTP 402 + parseable x402.
    payable = at least one complete CURRENT observed payment option.
    invocable = payable + input schema. Fail closed. Never fill from catalog.
    """
    env = envelope if isinstance(envelope, dict) else result.get("envelope")
    if isinstance(env, dict):
        result["envelope"] = env
    target = build_target(item, env)
    result["target"] = target
    schema, source = extract_input_schema_source(item, env)
    has_schema = isinstance(schema, dict) and bool(schema.get("properties") or schema.get("required"))
    live = bool(result.get("live"))
    result["challenge_observed"] = bool(live)
    result["payable"] = bool(live and select._is_payable(result))
    result["invocable"] = bool(result["payable"] and has_schema)
    if result["invocable"] and source:
        result["schema_source"] = source
        target["schema_source"] = source
    if result["payable"] and not result["invocable"]:
        result["miss_reason"] = "no_input_schema"
    elif not live:
        result["invocable"] = False
        result["payable"] = False
        result["challenge_observed"] = False
        if result.get("miss_reason"):
            result["miss_reason"] = public_miss_reason(result.get("miss_reason"))
        # Keep target (envelope-only accepts) so the caller sees the observed
        # handoff shape. Catalog prices stay on claimed, never here.
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProbeBlocked(Exception):
    """SSRF fail-closed. Must not be treated as a live upstream HTTP response."""


def catalog_known_item(url: str, catalog_item: dict | None = None) -> bool:
    """True when the URL is a catalog/fixture listing. Claims stay claimed."""
    if isinstance(catalog_item, dict) and catalog_item:
        return True
    raw = (url or "").strip()
    if not raw:
        return False
    if fixtures.fixture_mode():
        try:
            return bool(fixtures.lookup_url(raw))
        except Exception:
            return False
    try:
        from live402 import catalog as catalog_mod

        return bool(catalog_mod.claimed_item_for_url(raw))
    except Exception:
        return False


def direct_url_allowed(url: str, catalog_item: dict | None = None) -> bool:
    """Direct URL probe gate.

    Policy: HTTPS only, no URL credentials, public DNS + IP pin (existing SSRF).
    Unknown direct URLs: destination port 443 only.
    Catalog-known listings: allow the HTTPS port already present on that listing
    so a known non-443 catalog endpoint is not broken. Do not invent ports.
    """
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    port = url_port(parsed)
    if port is None:
        return False
    known = catalog_known_item(raw, catalog_item)
    if port != 443 and not known:
        return False
    return True


def url_port(parsed) -> int | None:
    """Destination port or None if invalid/out of range. Never raise ValueError."""
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return 443
    try:
        dest = int(port)
    except (TypeError, ValueError):
        return None
    if dest < 1 or dest > 65535:
        return None
    return dest


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


def _dns_executor() -> ThreadPoolExecutor:
    global _dns_pool
    with _dns_lock:
        if _dns_pool is None:
            _dns_pool = ThreadPoolExecutor(
                max_workers=MAX_DNS_WORKERS,
                thread_name_prefix="dns",
            )
        return _dns_pool


def dns_worker_cap() -> int:
    return MAX_DNS_WORKERS


def reset_dns_pool() -> None:
    """Drop the resolver pool so timed-out workers cannot pin later tests."""
    global _dns_pool
    with _dns_lock:
        old = _dns_pool
        _dns_pool = None
    if old is not None:
        old.shutdown(wait=False, cancel_futures=True)


def _getaddrinfo_timed(host: str, timeout: float | None = None, port: int = 443):
    """Bounded getaddrinfo. Timed-out lookups do not spawn extra threads."""
    cap = DNS_TIMEOUT if timeout is None else float(timeout)
    if cap <= 0:
        raise TimeoutError("getaddrinfo timed out")
    try:
        dest_port = int(port)
    except (TypeError, ValueError):
        dest_port = 443
    if dest_port <= 0 or dest_port > 65535:
        dest_port = 443
    fut = _dns_executor().submit(
        socket.getaddrinfo, host, dest_port, 0, socket.SOCK_STREAM
    )
    try:
        return fut.result(timeout=cap)
    except FuturesTimeout:
        raise TimeoutError("getaddrinfo timed out") from None
    except Exception:
        raise


def _checked_addrs(host: str, port: int = 443) -> list[tuple] | None:
    """Resolve once. Return sockaddrs only if every address is public. Else None."""
    if not host:
        return None
    try:
        dest_port = int(port)
    except (TypeError, ValueError):
        dest_port = 443
    if dest_port <= 0 or dest_port > 65535:
        return None
    literal = _try_ip(host)
    if literal is not None:
        if _ip_blocked(literal):
            return None
        return [(str(literal), dest_port)]
    if _host_name_blocked(host):
        return None
    try:
        infos = _getaddrinfo_timed(host, port=dest_port)
    except (OSError, TimeoutError):
        return None
    if not infos:
        return None
    addrs: list[tuple] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return None
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        if _ip_blocked(ip):
            return None
        if ip_str in seen:
            continue
        seen.add(ip_str)
        extra = sockaddr[2:] if len(sockaddr) > 2 else ()
        addrs.append((ip_str, dest_port) + extra)
    return addrs or None


def _resolve_public(host: str) -> bool:
    """DNS-resolve host and reject unless every address is a public IP. Fail closed."""
    return _checked_addrs(host) is not None


def _pin_https_target(url: str) -> tuple[str, list[tuple]] | None:
    """HTTPS + public DNS, with addresses to pin. None if blocked. Fail closed."""
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
    port = url_port(parsed)
    if port is None:
        return None
    addrs = _checked_addrs(host, port)
    if not addrs:
        return None
    return raw, addrs


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
    """Same allowlisted hosts as CATALOGS. Request-time query lives in catalog.py."""
    return CATALOGS


def safe_target(url: str) -> str | None:
    """Return the URL if it is https to a public host, else None. Fail closed."""
    pinned = _pin_https_target(url)
    return pinned[0] if pinned else None


def _https_url(url: str) -> str | None:
    """Scheme/userinfo gate used when ranking catalogs. Full SSRF is safe_target()."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    if url_port(parsed) is None:
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


def normalize_networks(raw) -> tuple[str, ...] | None:
    """Restrict searchable rails. Missing → None (unrestricted).

    Explicit empty or invalid never becomes all networks.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return ()
    seen: set[str] = set()
    rails: list[str] = []
    for item in items:
        name = normalize_prefer_network(item if isinstance(item, str) else str(item).strip())
        if not name or name in seen:
            continue
        seen.add(name)
        rails.append(name)
    return tuple(rail for rail in PREFER_NETWORKS if rail in seen)


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
        pinned = _pin_https_target(joined)
        if not pinned:
            raise ProbeBlocked("blocked redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is None:
            raise ProbeBlocked("blocked redirect")
        nxt.ssrf_hops = hops
        nxt.pinned_addrs = pinned[1]
        nxt.pinned_host = _hostname(urlparse(joined))
        try:
            nxt.remove_header("Host")
        except Exception:
            pass
        return nxt


class _BlockedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        raise ProbeBlocked("http not allowed")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP to the SSRF-checked IP. TLS SNI and HTTP Host stay the original name."""

    def __init__(self, host, *args, pinned_addrs=None, server_hostname=None, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._pinned_addrs = list(pinned_addrs or [])
        self._server_hostname = server_hostname
        if not self._server_hostname:
            raw = (host or "").split("%")[0]
            if raw.startswith("["):
                end = raw.find("]")
                raw = raw[1:end] if end > 0 else raw
            else:
                raw = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
            self._server_hostname = raw.strip().rstrip(".").lower()

    def connect(self):
        if not self._pinned_addrs:
            raise ProbeBlocked("pin missing")
        hostname = self._server_hostname
        if not hostname:
            raise ProbeBlocked("sni missing")
        context = self._context
        if context is None:
            raise ProbeBlocked("no tls context")
        last_err = None
        sock = None
        for sockaddr in self._pinned_addrs:
            ip = sockaddr[0]
            try:
                sock = socket.create_connection(
                    (ip, self.port),
                    self.timeout,
                    self.source_address,
                )
                break
            except OSError as exc:
                last_err = exc
                continue
        if sock is None:
            if last_err is not None:
                raise last_err
            raise ProbeBlocked("pin connect failed")
        try:
            self.sock = context.wrap_socket(sock, server_hostname=hostname)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        applied = getattr(self.sock, "server_hostname", None)
        if applied != hostname:
            try:
                self.sock.close()
            except Exception:
                pass
            raise ProbeBlocked("sni not applied")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        parsed = urlparse(req.full_url)
        hostname = _hostname(parsed)
        if not hostname:
            raise ProbeBlocked("no host")
        port = url_port(parsed)
        if port is None:
            raise ProbeBlocked("invalid port")
        addrs = getattr(req, "pinned_addrs", None)
        if not addrs:
            addrs = _checked_addrs(hostname, port)
        if not addrs:
            raise ProbeBlocked("ssrf")
        host_header = hostname
        if port != 443:
            host_header = "%s:%s" % (hostname, port)
        existing = req.get_header("Host")
        if existing:
            exist_host = existing.split(":")[0].strip("[]").lower()
            if exist_host != hostname:
                raise ProbeBlocked("host header mismatch")
        else:
            req.add_unredirected_header("Host", host_header)
        pinned = list(addrs)
        server_name = hostname

        def factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                pinned_addrs=pinned,
                server_hostname=server_name,
                **kwargs,
            )

        return self.do_open(factory, req)


def _opener():
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        _BlockedHTTPHandler(),
        _SSRFRedirectHandler(),
        _PinnedHTTPSHandler(context=ctx),
    )


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
    from live402.http_body import reject_json_constant

    for item in candidates:
        try:
            payload = json.loads(item, parse_constant=reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
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
            from live402.http_body import reject_json_constant

            parsed = json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant)
            if isinstance(parsed, dict):
                body_env = parsed
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
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
    """Catalog-declared seller body. Detection only — never sent on unpaid probes."""
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


def _catalog_requires_body(item: dict | None) -> bool:
    """True if the catalog says a request body is required to reach the endpoint."""
    return _declared_input_body(item) is not None


def _catalog_declares_post(item: dict | None) -> bool:
    """True only when the catalog explicitly declares POST. extract_method defaults are ignored."""
    if not isinstance(item, dict) or not item:
        return False
    raw = item.get("method")
    if isinstance(raw, str) and raw.strip().upper() == "POST":
        return True
    for bazaar in _bazaar_blobs(item, None):
        info = bazaar.get("info") or {}
        inp = info.get("input") or {}
        if isinstance(inp, dict):
            method = str(inp.get("method") or "").strip().upper()
            if method == "POST":
                return True
    return False


def _post_empty_justified(get_snap: dict | None, catalog_item: dict | None = None) -> bool:
    """POST {} only if GET 405/501 AND catalog declares POST AND no required body."""
    if not get_snap or get_snap.get("live"):
        return False
    miss = get_snap.get("miss_reason")
    if miss in {"ssrf", "probe_timeout"}:
        return False
    status = get_snap.get("status")
    if get_snap.get("has_402_challenge") or status == 402:
        return False
    if status not in {405, 501}:
        return False
    if _catalog_requires_body(catalog_item):
        return False
    if not _catalog_declares_post(catalog_item):
        return False
    return True


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


def _payto_matches_catalog(probed, item: dict | None, rail=None) -> bool:
    """True if probed payTo matches any catalog accept on that accept's rail."""
    if not probed or not item or not isinstance(item, dict):
        return False
    accepts = item.get("accepts") or []
    if isinstance(accepts, list) and accepts:
        for acc in accepts:
            if not isinstance(acc, dict):
                continue
            pay = acc.get("payTo")
            if not pay:
                continue
            acc_rail = payment.rail_of_network(acc.get("network") or "") or rail
            if payment.payto_equal(probed, pay, acc_rail):
                return True
        return False
    catalog_pay = _catalog_payto(item)
    return bool(catalog_pay and payment.payto_equal(probed, catalog_pay, rail))


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


def _catalog_amount(item: dict | None) -> str | None:
    if not item or not isinstance(item, dict):
        return None
    accepts = item.get("accepts") or []
    if isinstance(accepts, list):
        for acc in accepts:
            if not isinstance(acc, dict):
                continue
            for key in ("amount", "maxAmountRequired"):
                val = acc.get(key)
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
    return None


def _catalog_schema_present(item: dict | None) -> bool:
    if not item or not isinstance(item, dict):
        return False
    if item.get("_input_schema_present"):
        return True
    contract = item.get("_claimed_contract")
    if isinstance(contract, dict) and contract.get("origin") == "catalog_claimed":
        if contract.get("input_schema") or item.get("inputSchema"):
            return True
    schema = item.get("inputSchema")
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required") or schema.get("type")):
        return True
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    info = bazaar.get("info") or {}
    inp = info.get("input") or {}
    return bool(isinstance(inp, dict) and inp)


def _catalog_facilitator(item: dict | None) -> str | None:
    if not item or not isinstance(item, dict):
        return None
    accepts = item.get("accepts") or []
    if not isinstance(accepts, list):
        return None
    for acc in accepts:
        if not isinstance(acc, dict):
            continue
        extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
        fac = extra.get("facilitator")
        if fac and str(fac).strip():
            return str(fac).strip()
    return None


def attach_catalog_fields(result: dict, item: dict | None = None) -> dict:
    result["traction"] = _traction(item)
    result.setdefault("payTo", None)
    catalog_pay = _catalog_payto(item)
    probed = result.get("payTo")
    if catalog_pay:
        rail = result.get("rail") or _item_rail(item)
        mismatched = bool(probed and not _payto_matches_catalog(probed, item, rail))
        if mismatched:
            result["payTo_changed"] = True
        else:
            result.setdefault("payTo_changed", False)
    claimed = {}
    if catalog_pay:
        claimed["payTo"] = catalog_pay
    amt = _catalog_amount(item)
    if amt is not None:
        claimed["amount"] = amt
    if _catalog_schema_present(item):
        claimed["schema_present"] = True
    contract = item.get("_claimed_contract") if isinstance(item, dict) else None
    if isinstance(contract, dict) and contract.get("origin") == "catalog_claimed":
        claimed["schema_present"] = True
        claimed["contract"] = {
            "origin": "catalog_claimed",
            "method": contract.get("method"),
            "content_type": contract.get("content_type"),
            "tool_name": contract.get("tool_name"),
            "type": contract.get("type"),
            "schema_bytes": contract.get("schema_bytes"),
            "truncated": bool(contract.get("truncated")),
        }
    fac = _catalog_facilitator(item)
    if fac:
        claimed["facilitator"] = fac
        claimed["source"] = fac
    rail = item.get("_rail") if isinstance(item, dict) else None
    if rail and str(rail).strip():
        claimed.setdefault("source", str(rail).strip())
    catalog_acc = _catalog_accepts(item)
    if catalog_acc:
        claimed["accepts"] = catalog_acc
        claimed["payment_options"] = payment.payment_options_from_accepts(catalog_acc)
    if claimed:
        result["claimed"] = claimed
    return result


def _finalize_probe(result: dict, batch_id: str | None = None, record: bool = True) -> dict:
    """Record history and attach freshness/readiness. Never raises.

    record=False: do not write 402signal_observed (unpaid validate and route workers).
    Route persistence is the coordinator's job after the tranche is finalized.
    """
    try:
        from live402 import history as history_mod
        bid = batch_id or result.get("batch_id")
        if not bid:
            bid = uuid.uuid4().hex
        result["batch_id"] = bid
        if record:
            meta = history_mod.record_probe(result.get("url") or "", result)
            return history_mod.attach_to_result(result, meta)
        url = result.get("url") or ""
        summ = history_mod.summary(url) if url else history_mod._empty_summary()
        result["verified_at"] = result.get("probed_at")
        result["verified_seconds_ago"] = 0
        n_7d = int(summ.get("n_7d") or 0)
        result["readiness"] = history_mod.compute_readiness(result, n_7d)
        result["readiness_healthy"] = None
        result["history"] = summ
        if "claimed" not in result or not isinstance(result.get("claimed"), dict):
            result["claimed"] = history_mod._empty_claimed()
        if "observed" not in result or not isinstance(result.get("observed"), dict):
            obs = history_mod._empty_observed()
            obs["http_status"] = result.get("status")
            obs["payTo"] = result.get("payTo")
            obs["latency_ms"] = result.get("latency_ms")
            result["observed"] = obs
        try:
            from live402 import reputation as reputation_mod

            reputation_mod.attach(result)
        except Exception:
            pass
        return result
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


def _one_request(
    url: str,
    method: str,
    data: bytes | None = None,
    deadline: float | None = None,
    pinned_addrs: list[tuple] | None = None,
) -> dict:
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
    parsed = urlparse(url)
    dest_port = url_port(parsed)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or dest_port is None:
        return {
            "live": False,
            "status": None,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": "ssrf",
            "envelope": None,
        }
    host = _hostname(parsed)
    addrs = pinned_addrs if pinned_addrs else _checked_addrs(host, dest_port)
    if not host or not addrs:
        return {
            "live": False,
            "status": None,
            "has_402_challenge": False,
            "payTo": None,
            "miss_reason": "ssrf",
            "envelope": None,
        }
    timeout = _request_timeout(deadline)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    req.ssrf_hops = 0
    req.pinned_addrs = list(addrs)
    req.pinned_host = host
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


def _fixture_observed_envelope(row: dict | None, canned: dict) -> dict | None:
    """Canned 402 for fixture mode. Observed only — never merge a second catalog item."""
    if isinstance(canned.get("envelope"), dict) and canned["envelope"].get("accepts"):
        return canned["envelope"]
    accepts = row.get("accepts") if isinstance(row, dict) else None
    if not isinstance(accepts, list) or not accepts:
        return None
    env_accepts = []
    pay = canned.get("payTo")
    for acc in accepts:
        if not isinstance(acc, dict):
            continue
        row_acc = dict(acc)
        if pay and not str(row_acc.get("payTo") or "").strip():
            row_acc["payTo"] = pay
        env_accepts.append(row_acc)
    if not env_accepts:
        return None
    return {"x402Version": 2, "accepts": env_accepts}


def _fixture_probe(url: str, catalog_item: dict | None = None, batch_id: str | None = None, record: bool = True) -> dict:
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
            ),
            batch_id=batch_id,
            record=record,
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
    env = _fixture_observed_envelope(row, canned) if payable else None
    result = health_from_probe(row.get("url") or url, canned)
    if env:
        result["envelope"] = env
    result = attach_catalog_fields(result, catalog_item or row)
    result = attach_invocable_target(result, catalog_item or row, env)
    return _finalize_probe(result, batch_id=batch_id, record=record)


def probe_url(url: str, catalog_item: dict | None = None, deadline: float | None = None, batch_id: str | None = None, record: bool = True) -> dict:
    """Unpaid dual probe. Live = HTTP 402 with a parseable payment envelope."""
    if deadline is None:
        deadline = clock.monotonic() + PROBE_BUDGET_SECONDS
    bid = batch_id or uuid.uuid4().hex
    if fixtures.fixture_mode():
        return _fixture_probe(url, catalog_item, batch_id=bid, record=record)

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
        return _finalize_probe(attach_invocable_target(result, catalog_item), batch_id=bid, record=record)

    pinned = _pin_https_target(url)
    if not pinned:
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
        return _finalize_probe(attach_invocable_target(result, catalog_item), batch_id=bid, record=record)
    safe, addrs = pinned

    start = time.perf_counter()
    probes: list[dict] = []

    get_snap = _one_request(safe, "GET", deadline=deadline, pinned_addrs=addrs)
    probes.append(_probe_entry("GET", get_snap))
    if get_snap.get("miss_reason") == "ssrf":
        latency_ms = int((time.perf_counter() - start) * 1000)
        get_snap["latency_ms"] = latency_ms
        get_snap["probes"] = probes
        get_snap["probed_at"] = now_iso()
        result = health_from_probe(safe, get_snap)
        return _finalize_probe(attach_invocable_target(result, catalog_item), batch_id=bid, record=record)

    post_snap = None
    if not get_snap.get("live") and _post_empty_justified(get_snap, catalog_item):
        post_snap = _one_request(safe, "POST", data=b"{}", deadline=deadline, pinned_addrs=addrs)
        if get_snap.get("status") in {405, 501} and not post_snap.get("live"):
            post_snap["miss_reason"] = "no_402_envelope"
        probes.append(_probe_entry("POST", post_snap))

    unpaid_live = bool(get_snap.get("live") or (post_snap and post_snap.get("live")))

    winner = None
    if get_snap.get("live") and unpaid_live:
        winner = get_snap
    elif post_snap and post_snap.get("live") and unpaid_live:
        winner = post_snap
    else:
        winner = post_snap or get_snap

    live = bool(unpaid_live and winner and winner.get("live"))
    miss_reason = None
    if not live:
        get_miss = get_snap.get("miss_reason")
        if get_miss == "ssrf":
            miss_reason = "ssrf"
        elif get_miss == "probe_timeout" and post_snap is None:
            miss_reason = "probe_timeout"
        elif _catalog_requires_body(catalog_item):
            miss_reason = "unsafe_to_probe"
        elif get_snap.get("status") in {405, 501} and not (post_snap and post_snap.get("live")):
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
    return _finalize_probe(result, batch_id=bid, record=record)


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


_discovery_tls = threading.local()


def last_discovery_contracts() -> dict:
    """Request-local claimed contracts from the last fetch_discovery. Not RAM index."""
    got = getattr(_discovery_tls, "contracts", None)
    return got if isinstance(got, dict) else {}


def fetch_discovery(
    need: str = "",
    prefer_network: str | None = None,
    networks=None,
    limit: int = 20,
) -> list[dict]:
    """Need-scoped discovery. Never loads a 44k in-process index.

    prefer_network is ranking-only (passed through for callers). networks
    restricts which rails are queried. limit is kept for compat.
    Slim items only. Claimed schemas stay in a request-local stash for
    finalist hydration after rank.
    """
    _ = limit
    from live402 import catalog as catalog_mod
    working = catalog_mod.query_for_need(
        need, prefer_network=prefer_network, networks=networks
    )
    items = list(working.get("items") or [])
    contracts = working.get("_contracts") if isinstance(working, dict) else None
    _discovery_tls.contracts = contracts if isinstance(contracts, dict) else {}
    return items


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
        "discovery_matches": 0,
        "candidates_discovered": 0,
        "candidates_considered": 0,
        "candidates_probed": 0,
        "probe_ceiling": STANDARD_PROBE_CAP,
        "probe_budget_exhausted": False,
        "candidate_evaluation_complete": True,
        "stop_reason": "candidate_set_exhausted",
        "health": {
            "live": False,
            "last_probe": probed_at,
            "latency_ms": None,
            "has_402_challenge": False,
            "status": None,
        },
    }


def _align_target_with_selected(result: dict, selected: dict | None) -> None:
    """Handoff amount/facilitator must be the same observed option as selected_payment."""
    if not isinstance(result, dict) or not isinstance(selected, dict):
        return
    target = result.get("target") if isinstance(result.get("target"), dict) else None
    if target is None:
        return
    if selected.get("amount_atomic") is not None:
        target["amountAtomic"] = str(selected["amount_atomic"])
    if selected.get("display_amount"):
        target["displayAmount"] = selected["display_amount"]
    if selected.get("facilitator"):
        target["facilitator"] = selected["facilitator"]


def _attach_selection(body: dict, probed: list, winner, objective: str, constraints=None) -> dict:
    body["objective"] = objective
    if isinstance(winner, dict):
        selected = select.pick_selected_payment(winner, objective, constraints)
        if selected:
            winner["selected_payment"] = selected
            body["selected_payment"] = selected
            _align_target_with_selected(winner, selected)
        if winner.get("reputation"):
            body["reputation"] = winner["reputation"]
        elif selected or winner.get("url"):
            try:
                from live402 import reputation as reputation_mod

                reputation_mod.attach(winner)
                if winner.get("reputation"):
                    body["reputation"] = winner["reputation"]
            except Exception:
                pass
    body["compared"] = select.comparison(probed, winner, objective, constraints)
    body["tried"] = len(probed)
    return body


def _probed_urls(probed: list) -> set:
    return {
        (r or {}).get("url")
        for r in probed
        if isinstance(r, dict) and (r or {}).get("url")
    }


def _candidate_evaluation_complete(ranked: list, probed: list) -> bool:
    """True iff every ranked candidate in THIS request's working set was probed."""
    probed_urls = _probed_urls(probed)
    for item in ranked or []:
        url = _resource_url(item) if isinstance(item, dict) else None
        if url and url not in probed_urls:
            return False
    return True


def _stop_reason(
    *,
    winner,
    ranked: list,
    probed: list,
    probe_budget_exhausted: bool,
    some_live: bool,
    probe_ceiling: int | None = None,
) -> str:
    if winner:
        return "winner_selected"
    complete = _candidate_evaluation_complete(ranked, probed)
    untested = not complete
    if probe_budget_exhausted and untested:
        return "probe_budget_exhausted"
    cap = PROBE_CEILING if probe_ceiling is None else int(probe_ceiling)
    if untested and len(probed) >= cap:
        return "probe_limit_reached"
    if some_live:
        return "constraints_unmet"
    if complete:
        return "candidate_set_exhausted"
    if untested:
        return "probe_budget_exhausted" if probe_budget_exhausted else "probe_limit_reached"
    return "candidate_set_exhausted"


def _attach_route_funnel(
    body: dict,
    *,
    discovery_matches: int,
    candidates_considered: int,
    candidates_probed: int,
    probe_budget_exhausted: bool,
    candidate_evaluation_complete: bool,
    stop_reason: str,
    candidates_discovered: int | None = None,
    probe_ceiling: int | None = None,
) -> dict:
    body["discovery_matches"] = int(discovery_matches)
    body["candidates_considered"] = int(candidates_considered)
    body["candidates_probed"] = int(candidates_probed)
    body["candidates_discovered"] = int(
        candidates_discovered if candidates_discovered is not None else discovery_matches
    )
    body["probe_ceiling"] = int(probe_ceiling if probe_ceiling is not None else STANDARD_PROBE_CAP)
    body["probe_budget_exhausted"] = bool(probe_budget_exhausted)
    body["candidate_evaluation_complete"] = bool(candidate_evaluation_complete)
    body["stop_reason"] = stop_reason
    return body


def _selection_set(probed: list, constraints: dict | None = None) -> list:
    """Live hits. First unexpected payTo change is not selectable.

    A later second observation of the same dest clears payTo_pending
    (established). accept_payTo_change opts into first-change selection.
    Catalog claimed vs observed (payTo_changed) stays in the set.
    All-pending windows return empty unless that opt-in is set.
    """
    live_hits = [r for r in probed if isinstance(r, dict) and r.get("live")]
    cons = constraints if isinstance(constraints, dict) else {}
    if not cons.get("accept_payTo_change"):
        live_hits = [r for r in live_hits if not r.get("payTo_pending")]
    if any(not r.get("payTo_changed") for r in live_hits):
        return [r for r in live_hits if not r.get("payTo_changed")]
    return live_hits


# History may reorder only among close need scores. Wider than one token
# hit (~10) so a near-tie can move; narrower than a capability match (100).
HISTORY_CLOSE_SCORE = 20
HISTORY_FRESH_STRONG_S = 5 * 60
HISTORY_FRESH_USEFUL_S = 60 * 60
HISTORY_FRESH_WEAK_S = 24 * 60 * 60


def _success_freshness_band(last_success_402, now: int | None = None) -> int:
    """0 older/none, 1 <24h weak, 2 <1h useful, 3 <5m strong. No ML."""
    if last_success_402 is None or last_success_402 == "":
        return 0
    try:
        ts = int(last_success_402)
    except (TypeError, ValueError):
        return 0
    if now is None:
        now = int(time.time())
    age = now - ts
    if age < 0:
        age = 0
    if age < HISTORY_FRESH_STRONG_S:
        return 3
    if age < HISTORY_FRESH_USEFUL_S:
        return 2
    if age < HISTORY_FRESH_WEAK_S:
        return 1
    return 0


def _history_boost_shortlist(
    ranked: list[dict],
    need: str = "",
    prefer_network: str | None = None,
) -> list[dict]:
    """Cheap sqlite join: history reorders only among close need scores.

    Capability/need score and original rank stay primary. Stale last_success_402
    is historical context only and cannot leapfrog a substantially better match.
    prefer_network still groups first when requested; otherwise rail-neutral.
    """
    if len(ranked) <= 1:
        return ranked
    window = min(len(ranked), max(STANDARD_PROBE_CAP * 2, FIRST_TRANCHE * 2))
    head = ranked[:window]
    tail = ranked[window:]
    urls = [_resource_url(item) for item in head]
    try:
        from live402 import history as history_mod
        hints = history_mod.rank_hints(urls)
    except Exception:
        return ranked
    if not hints:
        return ranked
    prefer = normalize_prefer_network(prefer_network)
    now = int(time.time())
    meta = []
    for idx, item in enumerate(head):
        hint = hints.get(_resource_url(item)) or {}
        n_7d = 0
        try:
            n_7d = int(hint.get("n_7d") or 0)
        except (TypeError, ValueError):
            n_7d = 0
        rail = _item_rail(item)
        meta.append(
            {
                "idx": idx,
                "item": item,
                "score": score_need(need, item) if need else 0,
                "prefer_hit": 1 if prefer and rail == prefer else 0,
                "fresh": _success_freshness_band(hint.get("last_success_402"), now),
                "mature": 1 if n_7d >= 10 else 0,
                "weak": 1 if n_7d >= 3 else 0,
                "n_7d": n_7d,
            }
        )

    def reorder_group(group: list[dict]) -> list[dict]:
        if not group:
            return []
        # Score stays primary: cluster from the best need score down so a
        # substantially better match cannot sit behind a worse one.
        group = sorted(group, key=lambda row: (-row["score"], row["idx"]))
        clusters: list[list[dict]] = []
        current = [group[0]]
        head_score = group[0]["score"]
        for row in group[1:]:
            if abs(head_score - row["score"]) < HISTORY_CLOSE_SCORE:
                current.append(row)
            else:
                clusters.append(current)
                current = [row]
                head_score = row["score"]
        clusters.append(current)
        out: list[dict] = []
        for cluster in clusters:
            cluster.sort(
                key=lambda row: (
                    row["fresh"],
                    row["mature"],
                    row["weak"],
                    row["n_7d"] if (row["mature"] or row["weak"]) else 0,
                    -row["idx"],
                ),
                reverse=True,
            )
            out.extend(cluster)
        return out

    preferred = [row for row in meta if row["prefer_hit"]]
    other = [row for row in meta if not row["prefer_hit"]]
    return [row["item"] for row in reorder_group(preferred) + reorder_group(other)] + tail


def _finalize_routed_probe(result: dict, item: dict, need: str) -> dict:
    result = attach_catalog_fields(result, item)
    try:
        from live402 import history as history_mod
        result = history_mod.attach_to_result(result)
    except Exception:
        if result.get("payTo_changed"):
            result["risk"] = ["payTo_changed"]
    result["need"] = need
    result["rail"] = _item_rail(item)
    result["source"] = "fixture" if fixtures.fixture_mode() else "discovery"
    return result


def _probe_one_candidate(item: dict, need: str, deadline: float | None) -> dict:
    url = _resource_url(item)
    result = probe_url(url, catalog_item=item, deadline=deadline, record=False)
    return _finalize_routed_probe(result, item, need)


def _probe_miss_stub(item: dict, need: str, miss_reason: str) -> dict:
    url = _resource_url(item)
    return {
        "live": False,
        "invocable": False,
        "url": url,
        "status": None,
        "latency_ms": 0,
        "has_402_challenge": False,
        "payTo": None,
        "miss_reason": public_miss_reason(miss_reason) or miss_reason,
        "probes": [],
        "probed_at": now_iso(),
        "need": need,
        "rail": _item_rail(item) if isinstance(item, dict) else None,
        "source": "fixture" if fixtures.fixture_mode() else "discovery",
    }


def _run_routed_probe(item: dict, need: str, deadline: float | None) -> dict:
    """Worker: probe with record=False. Never persist. Never use the route batch_id."""
    url = _resource_url(item)
    host = _probe_host(url)
    if not acquire_probe_slot(host, deadline):
        return _probe_miss_stub(item, need, "probe_budget_exhausted")
    try:
        return _probe_one_candidate(item, need, deadline)
    except Exception:
        return _probe_miss_stub(item, need, "no_402_envelope")
    finally:
        release_probe_slot(host)


def _probe_one_or_stub(item: dict, need: str, deadline: float | None) -> dict:
    return _run_routed_probe(item, need, deadline)


def _probe_tranche(
    items: list[dict],
    need: str,
    deadline: float | None,
    should_stop=None,
) -> list[dict]:
    """Concurrent unpaid probes. max MAX_IN_FLIGHT per tranche, process-wide cap."""
    if not items:
        return []
    if remaining_timeout(deadline) is not None and remaining_timeout(deadline) <= 0:
        return []
    collected: list[dict] = []

    def pending_items(pending_futs, fut_map) -> list[dict]:
        return [fut_map[fut] for fut in pending_futs]

    def take(row: dict, still_pending: list[dict] | None = None) -> bool:
        collected.append(row)
        if not should_stop:
            return False
        try:
            return bool(should_stop(collected, still_pending or []))
        except TypeError:
            return bool(should_stop(collected))

    if len(items) == 1:
        if take(_probe_one_or_stub(items[0], need, deadline), []):
            return collected
        return collected

    pool = _shared_probe_pool()
    futs = {
        pool.submit(_run_routed_probe, item, need, deadline): item
        for item in items
    }
    pending = set(futs)
    while pending:
        if remaining_timeout(deadline) is not None and remaining_timeout(deadline) <= 0:
            for leftover in pending:
                leftover.cancel()
            break
        done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        rows = []
        for fut in done:
            item = futs[fut]
            try:
                rows.append(fut.result())
            except Exception:
                rows.append(_probe_miss_stub(item, need, "no_402_envelope"))
        leftover_items = pending_items(pending, futs)
        stop = False
        for row in rows:
            if take(row, leftover_items):
                stop = True
        if stop:
            for leftover in pending:
                leftover.cancel()
            pending.clear()
            return collected
    return collected


def _commit_route_batch(batch_id: str, probed: list) -> dict:
    """Coordinator: persist only the finalized selection set, then seal.

    Returns {url: write_meta} so the winner can be rehydrated with change state.
    """
    accepted = [row for row in probed if isinstance(row, dict)]
    for row in accepted:
        row["batch_id"] = batch_id
    try:
        from live402 import history as history_mod
        return history_mod.persist_route_batch(batch_id, accepted) or {}
    except Exception:
        return {}


def _budget_hit(deadline: float | None) -> bool:
    left = remaining_timeout(deadline)
    return left is not None and left <= 0


def route_need(
    need: str,
    deadline: float | None = None,
    prefer_network: str | None = None,
    objective: str | None = None,
    constraints: dict | None = None,
    search_depth: str | None = None,
    max_candidates_to_probe=None,
    probe_ceiling: int | None = None,
) -> dict:
    """Rank, probe in adaptive tranches under the 55s budget, pick best-of-N.

    Typical: first 3, then 2–4 more if no winner. Hard server ceiling is 20.
    Candidate #6 is reachable when 1–5 fail without every request doing 20.
    """
    if deadline is None:
        deadline = clock.monotonic() + PROBE_BUDGET_SECONDS
    prefer = normalize_prefer_network(prefer_network)
    obj = select.parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    rails = cons.get("rails") if isinstance(cons, dict) else None
    plan = probe_plan(
        search_depth=search_depth,
        max_candidates_to_probe=max_candidates_to_probe,
    )
    if probe_ceiling is not None:
        try:
            ceiling = min(max(int(probe_ceiling), 1), PROBE_CEILING)
        except (TypeError, ValueError):
            ceiling = plan["probe_ceiling"]
    else:
        ceiling = plan["probe_ceiling"]
    try:
        items = fetch_discovery(need, prefer_network=prefer, networks=rails)
    except Exception:
        miss = _discovery_unavailable_miss(obj)
        miss["probe_ceiling"] = ceiling
        return miss
    discovered = len(items)
    ranked = rank_resources(need, items, prefer_network=prefer)
    ranked = _history_boost_shortlist(ranked, need=need, prefer_network=prefer)
    try:
        from live402 import hydrate as hydrate_mod
        hydrate_mod.hydrate_finalists(ranked, stash=last_discovery_contracts())
    except Exception:
        pass
    discovery_matches = len(ranked)
    probed: list[dict] = []
    last = None
    batch_id = uuid.uuid4().hex
    next_idx = 0
    probe_budget_exhausted = False
    first_tranche = True

    rank_of = {_resource_url(item): idx for idx, item in enumerate(ranked)}

    def _by_rank(rows: list) -> list:
        """Preserve original shortlist order so equal live hits keep first-ranked."""
        return sorted(
            rows,
            key=lambda r: rank_of.get((r or {}).get("url") or "", 10**9),
        )

    def winner_now():
        return select.pick_winner(_by_rank(_selection_set(probed, cons)), obj, cons)

    while next_idx < len(ranked) and len(probed) < ceiling:
        if _budget_hit(deadline):
            probe_budget_exhausted = True
            break
        if select.enough_evidence(probed, obj, cons):
            break
        take = next_tranche_size(
            len(probed), len(ranked) - next_idx, ceiling, first_tranche
        )
        first_tranche = False
        if take <= 0:
            break
        tranche = ranked[next_idx:next_idx + take]
        batch = _probe_tranche(tranche, need, deadline)
        if batch:
            probed.extend(batch)
            last = batch[-1]
            next_idx += take
        elif _budget_hit(deadline):
            probe_budget_exhausted = True
            break
        else:
            next_idx += take
        if select.enough_evidence(probed, obj, cons):
            break
        if _budget_hit(deadline) and not _candidate_evaluation_complete(ranked, probed):
            probe_budget_exhausted = True
            break

    if _budget_hit(deadline) and not _candidate_evaluation_complete(ranked, probed) and not winner_now():
        probe_budget_exhausted = True

    winner = winner_now()
    evaluation_complete = _candidate_evaluation_complete(ranked, probed)
    some_live = any(isinstance(r, dict) and r.get("live") for r in probed)
    stop_reason = _stop_reason(
        winner=winner,
        ranked=ranked,
        probed=probed,
        probe_budget_exhausted=bool(probe_budget_exhausted),
        some_live=some_live,
        probe_ceiling=ceiling,
    )
    funnel = {
        "discovery_matches": discovery_matches,
        "candidates_discovered": discovered,
        "candidates_considered": discovery_matches,
        "candidates_probed": len(probed),
        "probe_ceiling": ceiling,
        "probe_budget_exhausted": bool(probe_budget_exhausted),
        "candidate_evaluation_complete": evaluation_complete,
        "stop_reason": stop_reason,
    }
    if winner:
        body = _attach_route_funnel(
            _attach_selection(winner, probed, winner, obj, cons),
            **funnel,
        )
        metas = _commit_route_batch(batch_id, probed)
        try:
            from live402 import history as history_mod
            meta = metas.get(body.get("url") or "") if isinstance(metas, dict) else None
            body = history_mod.attach_to_result(body, meta)
        except Exception:
            pass
        body["batch_id"] = batch_id
        return body
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
    untested = not evaluation_complete
    if not ranked:
        body["miss_reason"] = "no_candidates"
    elif probe_budget_exhausted and untested:
        body["miss_reason"] = "probe_budget_exhausted"
    elif untested and len(probed) >= ceiling:
        body["miss_reason"] = "probe_limit_reached"
    elif some_live:
        body["miss_reason"] = "constraints_unmet"
    elif last and last.get("miss_reason"):
        body["miss_reason"] = public_miss_reason(last.get("miss_reason")) or last.get("miss_reason")
    elif untested:
        body["miss_reason"] = "probe_budget_exhausted" if probe_budget_exhausted else "probe_limit_reached"
    if last:
        body["last"] = {
            "url": last.get("url"),
            "status": last.get("status"),
            "latency_ms": last.get("latency_ms"),
            "miss_reason": last.get("miss_reason"),
        }
        if last.get("rail"):
            body["rail"] = last.get("rail")
    out = _attach_route_funnel(_attach_selection(body, probed, None, obj, cons), **funnel)
    _commit_route_batch(batch_id, probed)
    out["batch_id"] = batch_id
    return out
