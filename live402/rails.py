"""GET /rails — pay-in networks. Not stuffed into /health. Cache cheaply."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from live402 import fixtures, payment, probe

CACHE_TTL = 30.0
PING_TIMEOUT = 1.5
USER_AGENT = "402Signal/0.1 (rails health; no payment)"

_lock = threading.Lock()
_collect_cv = threading.Condition(_lock)
_cache: dict = {"at": 0.0, "payload": None}
_collecting = False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ping must not follow Location (open redirect / SSRF). 3xx = down."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ping_opener():
    return urllib.request.build_opener(_NoRedirectHandler)


def reset_cache() -> None:
    global _collecting
    with _lock:
        _cache["at"] = 0.0
        _cache["payload"] = None
        _collecting = False
        _collect_cv.notify_all()


def _ping(url: str) -> tuple[bool, int | None]:
    """GET facilitator /supported. up = reachable HTTP, not a CDP-secret health check.

    Does not follow redirects. 4xx = up, 5xx = down, timeout/URLError = down.
    HTTPS + catalog_url_allowed only. Never returns error bodies.
    """
    raw = (url or "").strip()
    if not raw.startswith("https://"):
        return False, None
    if not probe.catalog_url_allowed(raw):
        return False, None
    start = time.perf_counter()
    req = urllib.request.Request(
        raw,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with _ping_opener().open(req, timeout=PING_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            try:
                resp.read(2048)
            except Exception:
                pass
        latency = int((time.perf_counter() - start) * 1000)
        up = isinstance(status, int) and 200 <= status < 500
        return up, latency
    except urllib.error.HTTPError as err:
        latency = int((time.perf_counter() - start) * 1000)
        try:
            # Drain only. Never return or parse error bodies.
            err.read(2048)
        except Exception:
            pass
        status = getattr(err, "code", None)
        # 4xx = reachable (e.g. unauthenticated 401). 5xx / 3xx = down.
        up = isinstance(status, int) and 400 <= status < 500
        return up, latency
    except (urllib.error.URLError, TimeoutError):
        latency = int((time.perf_counter() - start) * 1000)
        return False, latency
    except Exception:
        latency = int((time.perf_counter() - start) * 1000)
        return False, latency


def _supported_url(facilitator_base: str) -> str:
    return facilitator_base.rstrip("/") + "/supported"


def _rail_row(name: str, network: str, caip2: str, asset: str, pay_to: str, facilitator: str, fee_payer: str | None) -> dict:
    if fixtures.fixture_mode():
        up, latency = True, 1
    else:
        up, latency = _ping(_supported_url(facilitator))
    return {
        "network": name,
        "v1Network": name,
        "caip2": caip2,
        "asset": asset,
        "amountAtomic": payment.AMOUNT_ATOMIC,
        "displayAmount": payment.AMOUNT_USD,
        "facilitator": facilitator,
        "feePayer": fee_payer,
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
        "up": up,
        "latency_ms": latency,
    }


def collect() -> dict:
    rails = [
        _rail_row(
            "base",
            "base",
            payment.BASE_CAIP2,
            payment.USDC_BASE,
            payment.payto_address(),
            payment.CDP_FACILITATOR,
            None,
        ),
        _rail_row(
            "solana",
            "solana",
            payment.SOLANA_MAINNET,
            payment.USDC_SOLANA_MINT,
            payment.payto_solana(),
            payment.SOLANA_FACILITATOR,
            payment.SOLANA_FEE_PAYER,
        ),
        _rail_row(
            "algorand",
            "algorand",
            payment.ALGORAND_MAINNET,
            payment.USDC_ALGORAND_ASA,
            payment.payto_algorand(),
            payment.ALGORAND_FACILITATOR,
            payment.ALGORAND_FEE_PAYER,
        ),
    ]
    return {
        "ok": True,
        "asset": "USDC",
        "amountAtomic": payment.AMOUNT_ATOMIC,
        "displayAmount": payment.AMOUNT_USD,
        "maxTimeoutSeconds": 60,
        "updated_at": probe.now_iso(),
        "cached_s": CACHE_TTL,
        "note": "Copy each rail's facilitator URL from this document. Do not default to x402.org.",
        "rails": rails,
        "facilitators": [r["facilitator"] for r in rails],
        "feePayers": {r["network"]: r["feePayer"] for r in rails},
    }


def get_rails() -> dict:
    """Cached facilitator pings. Single-flight: one in-flight collect.

    Waiters reuse the in-flight result. If last-good exists and a collect is
    already running, return last-good immediately so ThreadingHTTPServer
    handlers do not stack pings. Never waits on a discovery crawl.
    """
    global _collecting
    now = time.monotonic()
    with _lock:
        payload = _cache.get("payload")
        if payload is not None and (now - _cache["at"]) < CACHE_TTL:
            return payload

    with _lock:
        payload = _cache.get("payload")
        now = time.monotonic()
        if payload is not None and (now - _cache["at"]) < CACHE_TTL:
            return payload
        if payload is not None and _collecting:
            return payload
        if _collecting:
            while _collecting:
                _collect_cv.wait()
            cached = _cache.get("payload")
            if cached is not None:
                return cached
            # reset_cache raced; become leader
        _collecting = True

    try:
        built = collect()
    except Exception:
        with _lock:
            _collecting = False
            _collect_cv.notify_all()
        raise
    with _lock:
        _cache["at"] = time.monotonic()
        _cache["payload"] = built
        _collecting = False
        _collect_cv.notify_all()
    return built
