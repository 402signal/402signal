"""Probe a URL. Never pays upstream. Never holds keys."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from live402 import fixtures

USER_AGENT = "402Signal/0.1 (fail-closed probe; no payment)"
DISCOVERY_URL = (
    "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=20"
)
CATALOGS = (
    ("base", "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=20"),
    ("solana", "https://facilitator.payai.network/discovery/resources?limit=20"),
    ("algorand", "https://facilitator.goplausible.xyz/discovery/resources?limit=20"),
)
CATALOG_HOSTS = frozenset(
    (urlparse(url).hostname or "").lower() for _, url in CATALOGS
)
PULSE_LIMIT = 100
CATALOG_READ_LIMIT = 524_288
DEFAULT_TIMEOUT = 4.0
MAX_PROBE = 5
READ_LIMIT = 65536
MAX_REDIRECTS = 2
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
        return float(os.environ.get("LIVE402_PROBE_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


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


def _resolve_public(host: str) -> bool:
    """DNS-resolve host and reject unless every address is a public IP. Fail closed."""
    literal = _try_ip(host)
    if literal is not None:
        return not _ip_blocked(literal)
    if _host_name_blocked(host):
        return False
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
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
    """Same allowlisted hosts as CATALOGS, higher limit for the dashboard only."""
    out = []
    for rail, url in CATALOGS:
        if re.search(r"limit=\d+", url):
            out.append((rail, re.sub(r"limit=\d+", f"limit={PULSE_LIMIT}", url)))
        else:
            sep = "&" if "?" in url else "?"
            out.append((rail, f"{url}{sep}limit={PULSE_LIMIT}"))
    return tuple(out)


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
    if not env or not isinstance(env, dict):
        return None
    accepts = env.get("accepts") or []
    if isinstance(accepts, list) and accepts:
        first = accepts[0]
        if isinstance(first, dict):
            val = first.get("payTo")
            if val and str(val).strip():
                return str(val).strip()
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
            return None, "http_200_no_challenge"
        if status == 400:
            return None, "http_400"
        if status == 404:
            return None, "http_404"
        if status == 405:
            return None, "http_405"
        if status == 501:
            return None, "http_501"
        if status is None:
            return None, "timeout"
        if 500 <= int(status) <= 599:
            return None, f"http_{status}"
        return None, f"http_{status}"

    envelope = header_env if _envelope_is_parseable(header_env) else None
    if envelope is None and body_env is not None:
        if _envelope_is_parseable(body_env):
            envelope = body_env

    if envelope is not None:
        return envelope, None

    empty_body = body_env is None or body_env == {}
    no_header = header_env is None or header_env == {}
    if empty_body and no_header:
        return None, "empty_402"
    if header_env or body_env:
        return None, "no_accepts"
    return None, "empty_402"


def _read_limited(fp) -> bytes:
    try:
        return fp.read(READ_LIMIT) if fp is not None else b""
    except Exception:
        return b""


def _miss_from_status(status: int | None) -> str:
    if status == 200:
        return "http_200_no_challenge"
    if status == 400:
        return "http_400"
    if status == 404:
        return "http_404"
    if status == 405:
        return "http_405"
    if status == 501:
        return "http_501"
    if status is None:
        return "timeout"
    return f"http_{status}"


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
    )
    blobs = [item]
    meta = item.get("metadata")
    if isinstance(meta, dict):
        blobs.append(meta)
        disc = meta.get("discovery")
        if isinstance(disc, dict):
            blobs.append(disc)
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
        result["payTo_changed"] = bool(
            probed and str(probed).strip().lower() != catalog_pay.lower()
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
        out["miss_reason"] = snap["miss_reason"]
    if snap.get("traction") is not None:
        out["traction"] = snap["traction"]
    if "payTo_changed" in snap:
        out["payTo_changed"] = snap["payTo_changed"]
    return out


def _probe_entry(method: str, snap: dict) -> dict:
    entry = {"method": method, "status": snap.get("status")}
    if not snap.get("live") and snap.get("miss_reason"):
        entry["miss_reason"] = snap["miss_reason"]
    return entry


def _one_request(url: str, method: str, data: bytes | None = None) -> dict:
    """Single unpaid HTTP probe. Never pays. ProbeBlocked is ssrf, never live."""
    timeout = probe_timeout()
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
        reason = "timeout"
        name = type(exc).__name__.lower()
        msg = str(getattr(exc, "reason", exc) or "").lower()
        if "timed out" in msg or "timeout" in name or "timeout" in msg:
            reason = "timeout"
        elif isinstance(exc, socket.timeout):
            reason = "timeout"
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
        return str(canned["miss_reason"])
    status = canned.get("status")
    if status == 200:
        return "http_200_no_challenge"
    if status == 402:
        return "empty_402"
    if status == 400:
        return "http_400"
    if status == 404:
        return "http_404"
    if status == 405:
        return "get_405_post_failed"
    if status is None:
        return "timeout"
    return f"http_{status}"


def _fixture_probe(url: str, catalog_item: dict | None = None) -> dict:
    row = fixtures.lookup_url(url)
    probed_at = now_iso()
    if not row:
        return health_from_probe(
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
    return attach_catalog_fields(result, catalog_item or row)


def probe_url(url: str, catalog_item: dict | None = None) -> dict:
    """Unpaid dual probe. Live = HTTP 402 with a parseable payment envelope."""
    if fixtures.fixture_mode():
        return _fixture_probe(url, catalog_item)

    safe = safe_target(url)
    if not safe:
        return health_from_probe(
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

    start = time.perf_counter()
    probes: list[dict] = []

    get_snap = _one_request(safe, "GET")
    probes.append(_probe_entry("GET", get_snap))
    if get_snap.get("miss_reason") == "ssrf":
        latency_ms = int((time.perf_counter() - start) * 1000)
        get_snap["latency_ms"] = latency_ms
        get_snap["probes"] = probes
        get_snap["probed_at"] = now_iso()
        return health_from_probe(safe, get_snap)

    post_snap = None
    if not get_snap.get("live"):
        post_snap = _one_request(safe, "POST", data=b"{}")
        if get_snap.get("status") in {405, 501} and not post_snap.get("live"):
            post_snap["miss_reason"] = "get_405_post_failed"
        probes.append(_probe_entry("POST", post_snap))

    declared_snap = None
    declared = _declared_input_body(catalog_item)
    if declared is not None:
        raw = json.dumps(declared, separators=(",", ":")).encode("utf-8")
        declared_snap = _one_request(safe, "POST", data=raw)
        probes.append(_probe_entry("POST", declared_snap))

    unpaid_live = bool(get_snap.get("live") or (post_snap and post_snap.get("live")))
    if declared_snap is not None:
        dstatus = declared_snap.get("status")
        dmiss = declared_snap.get("miss_reason")
        if dstatus == 200 or dmiss in {"empty_402", "http_200_no_challenge"}:
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
            }:
                winner = declared_snap

    live = bool(unpaid_live and winner and winner.get("live"))
    miss_reason = None
    if not live:
        if get_snap.get("status") in {405, 501} and not (post_snap and post_snap.get("live")):
            miss_reason = "get_405_post_failed"
        else:
            miss_reason = (winner or {}).get("miss_reason") or "timeout"

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
    result = health_from_probe(safe, snap)
    return attach_catalog_fields(result, catalog_item)


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
    blob = _resource_blob(item)
    hay = _tokens(blob)
    hit = q & hay
    score = len(hit) * 10
    low = blob.lower()
    for tok in q:
        if tok in low:
            score += 2
    return score


def rank_resources(need: str, items: list[dict]) -> list[dict]:
    ranked = []
    for item in items:
        url = _resource_url(item)
        if not _https_url(url) and not fixtures.fixture_mode():
            continue
        if fixtures.fixture_mode() and not url:
            continue
        s = score_need(need, item)
        if s <= 0:
            continue
        ranked.append((s, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked]


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


def _fetch_one_catalog(rail: str, url: str, timeout: float) -> list[dict]:
    if not catalog_url_allowed(url):
        return []
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
            return []
        raw = resp.read(CATALOG_READ_LIMIT + 1)
    if len(raw) > CATALOG_READ_LIMIT:
        return []
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, list):
        items = payload
    else:
        items = list(payload.get("items") or payload.get("resources") or [])
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
    timeout = max(probe_timeout(), 8.0)
    merged: list[dict] = []
    seen: set[str] = set()
    for rail, url in CATALOGS:
        try:
            items = _fetch_one_catalog(rail, url, timeout)
        except Exception:
            continue
        for item in items:
            key = _resource_url(item) or json.dumps(item, sort_keys=True)[:200]
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def route_need(need: str) -> dict:
    """Fuzzy-match discovery, probe up to 5, first payable 402 wins. Else fail-closed."""
    try:
        items = fetch_discovery()
    except Exception:
        return {
            "live": False,
            "url": None,
            "tried": 0,
            "error": "discovery_unavailable",
            "payTo": None,
            "traction": "unknown",
            "miss_reason": "discovery_unavailable",
            "probes": [],
            "probed_at": now_iso(),
            "health": {
                "live": False,
                "last_probe": now_iso(),
                "latency_ms": None,
                "has_402_challenge": False,
                "status": None,
            },
        }
    ranked = rank_resources(need, items)
    tried = 0
    last = None
    for item in ranked[:MAX_PROBE]:
        url = _resource_url(item)
        last = probe_url(url, catalog_item=item)
        last = attach_catalog_fields(last, item)
        tried += 1
        last["tried"] = tried
        last["need"] = need
        last["rail"] = _item_rail(item)
        last["source"] = "fixture" if fixtures.fixture_mode() else "discovery"
        # Crash-402 / 5xx / empty envelope are not live; try the next candidate.
        if last.get("live"):
            return last
    body = {
        "live": False,
        "url": None,
        "tried": tried,
        "need": need,
        "source": "fixture" if fixtures.fixture_mode() else "discovery",
        "payTo": None,
        "traction": (last or {}).get("traction") or "unknown",
        "probes": (last or {}).get("probes") or [],
        "status": (last or {}).get("status"),
        "latency_ms": (last or {}).get("latency_ms"),
        "has_402_challenge": bool((last or {}).get("has_402_challenge")),
        "probed_at": now_iso(),
        "health": {
            "live": False,
            "last_probe": (last or {}).get("probed_at") or now_iso(),
            "latency_ms": (last or {}).get("latency_ms"),
            "has_402_challenge": bool((last or {}).get("has_402_challenge")),
            "status": (last or {}).get("status"),
        },
    }
    if last and last.get("miss_reason"):
        body["miss_reason"] = last.get("miss_reason")
    elif tried == 0:
        body["miss_reason"] = "no_match"
    if last:
        body["last"] = {
            "url": last.get("url"),
            "status": last.get("status"),
            "latency_ms": last.get("latency_ms"),
            "miss_reason": last.get("miss_reason"),
        }
        if last.get("rail"):
            body["rail"] = last.get("rail")
    return body
