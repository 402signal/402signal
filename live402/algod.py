"""Pinned Algorand suggested params for the unpaid 402 extra.

Browser does not call a public algod. Host is hardcoded. No redirects.
Not a general HTTP client. rails.py / probe.py SSRF rules are untouched.

Payment-rail MainNet genesis only. This is not Falcon PQ broadcast.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

from live402 import clock, fixtures

# Pinned. Never take this URL from a request, 402 body, or env.
ALGOD_PARAMS_URL = "https://mainnet-api.algonode.cloud/v2/transactions/params"
ALGOD_HOST = "mainnet-api.algonode.cloud"
GENESIS_ID = "mainnet-v1.0"
GENESIS_HASH = "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
MIN_FEE = 1000
VALID_WINDOW = 1000
TIMEOUT = 2.0
CACHE_TTL = 15.0
FAILURE_TTL = 5.0
STALE_TTL = 120.0
USER_AGENT = "402Signal/0.1 (algod params; no payment keys)"

_lock = threading.Lock()
_cv = threading.Condition(_lock)
_cache: dict = {
    "at": 0.0,
    "payload": None,
    "fail_at": 0.0,
    "inflight": False,
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reset_cache() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["payload"] = None
        _cache["fail_at"] = 0.0
        _cache["inflight"] = False
        _cv.notify_all()


def _fixture_params() -> dict:
    return {
        "flatFee": True,
        "fee": MIN_FEE,
        "minFee": MIN_FEE,
        "firstRound": 50000000,
        "lastRound": 50000000 + VALID_WINDOW,
        "firstValid": 50000000,
        "lastValid": 50000000 + VALID_WINDOW,
        "genesisHash": GENESIS_HASH,
        "genesisID": GENESIS_ID,
    }


def _constants() -> dict:
    return {
        "flatFee": True,
        "fee": MIN_FEE,
        "minFee": MIN_FEE,
        "genesisHash": GENESIS_HASH,
        "genesisID": GENESIS_ID,
    }


def _genesis_exact(gid: str, gh: str) -> bool:
    return gid == GENESIS_ID and gh == GENESIS_HASH


def _fetch() -> dict | None:
    parsed = urlparse(ALGOD_PARAMS_URL)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != ALGOD_HOST:
        return None
    if parsed.username or parsed.password:
        return None
    req = urllib.request.Request(
        ALGOD_PARAMS_URL,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not isinstance(status, int) or status < 200 or status >= 300:
                return None
            raw = resp.read(4096)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    try:
        last = int(body.get("last-round") or body.get("lastRound") or 0)
        min_fee = int(body.get("min-fee") or body.get("minFee") or MIN_FEE)
    except (TypeError, ValueError):
        return None
    if last < 1:
        return None
    gh = str(body.get("genesis-hash") or body.get("genesisHash") or "").strip()
    gid = str(body.get("genesis-id") or body.get("genesisID") or "").strip()
    # Exact MainNet pin. A pinned host returning another genesis must not alter the challenge.
    if not _genesis_exact(gid, gh):
        return None
    if min_fee < 1:
        min_fee = MIN_FEE
    return {
        "flatFee": True,
        "fee": min_fee,
        "minFee": min_fee,
        "firstRound": last,
        "lastRound": last + VALID_WINDOW,
        "firstValid": last,
        "lastValid": last + VALID_WINDOW,
        "genesisHash": GENESIS_HASH,
        "genesisID": GENESIS_ID,
    }


def _refresh() -> dict | None:
    fetched = _fetch()
    now = clock.monotonic()
    with _lock:
        if fetched is not None:
            _cache["at"] = now
            _cache["payload"] = dict(fetched)
            _cache["fail_at"] = 0.0
        else:
            _cache["fail_at"] = now
        _cache["inflight"] = False
        _cv.notify_all()
        payload = _cache.get("payload")
        return dict(payload) if isinstance(payload, dict) else None


def _background_refresh() -> None:
    try:
        _refresh()
    except Exception:
        with _lock:
            _cache["inflight"] = False
            _cache["fail_at"] = clock.monotonic()
            _cv.notify_all()


def suggested_params() -> dict:
    """Return suggestedParams for the Algorand accept extra. Never raises."""
    if fixtures.fixture_mode():
        return _fixture_params()
    now = clock.monotonic()
    with _lock:
        payload = _cache.get("payload")
        at = float(_cache.get("at") or 0.0)
        fail_at = float(_cache.get("fail_at") or 0.0)
        if payload is not None and (now - at) < CACHE_TTL:
            return dict(payload)
        if fail_at and (now - fail_at) < FAILURE_TTL:
            if payload is not None:
                return dict(payload)
            return _constants()
        if _cache.get("inflight"):
            if payload is not None and (now - at) < STALE_TTL:
                return dict(payload)
            while _cache.get("inflight"):
                _cv.wait(timeout=0.2)
                if not _cache.get("inflight"):
                    break
            cached = _cache.get("payload")
            if cached is not None:
                return dict(cached)
            return _constants()
        stale_ok = payload is not None and (now - at) < STALE_TTL
        _cache["inflight"] = True
        if stale_ok:
            threading.Thread(
                target=_background_refresh,
                name="algod-refresh",
                daemon=True,
            ).start()
            return dict(payload)

    fetched = _refresh()
    if fetched is not None:
        return fetched
    return _constants()
