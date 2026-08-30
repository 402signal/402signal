"""Pinned Algorand suggested params for the unpaid 402 extra.

Browser does not call a public algod. Host is hardcoded. No redirects.
Not a general HTTP client. rails.py / probe.py SSRF rules are untouched.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from live402 import fixtures

# Pinned. Never take this URL from a request, 402 body, or env.
ALGOD_PARAMS_URL = "https://mainnet-api.algonode.cloud/v2/transactions/params"
ALGOD_HOST = "mainnet-api.algonode.cloud"
GENESIS_ID = "mainnet-v1.0"
GENESIS_HASH = "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
MIN_FEE = 1000
VALID_WINDOW = 1000
TIMEOUT = 2.0
CACHE_TTL = 15.0
USER_AGENT = "402Signal/0.1 (algod params; no payment keys)"

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "payload": None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reset_cache() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["payload"] = None


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
    gh = str(body.get("genesis-hash") or body.get("genesisHash") or GENESIS_HASH).strip()
    gid = str(body.get("genesis-id") or body.get("genesisID") or GENESIS_ID).strip()
    if not gh or not gid:
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
        "genesisHash": gh,
        "genesisID": gid,
    }


def suggested_params() -> dict:
    """Return suggestedParams for the Algorand accept extra. Never raises."""
    if fixtures.fixture_mode():
        return _fixture_params()
    now = time.monotonic()
    with _lock:
        payload = _cache.get("payload")
        if payload is not None and (now - _cache["at"]) < CACHE_TTL:
            return dict(payload)
    fetched = _fetch()
    out = fetched or _constants()
    if fetched is not None:
        with _lock:
            _cache["at"] = time.monotonic()
            _cache["payload"] = dict(out)
    return dict(out)
