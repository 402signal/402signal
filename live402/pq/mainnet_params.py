"""Pinned MainNet suggested-params reader for PQ canary policy.

Operator authorize/prepare fetches this snapshot itself. Caller cannot
select URL, fee, firstValid, or lastValid. HTTPS only, no redirects,
bounded body and timeout, exact MainNet genesis.

This is not the payment-rail live402.algod cache. That path never
raises and rewrites lastRound as lastValid. PQ policy needs the
algod last-round as lastRound and algod fee as fee-per-byte, and
must fail closed when the pinned host is unreadable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from live402.pq import network as netcfg

PARAMS_PATH = "/v2/transactions/params"
PARAMS_TIMEOUT_S = 5.0
PARAMS_MAX_BODY = 4096
USER_AGENT = "402Signal/0.1 (pq mainnet params; no keys in logs)"

# Approved MainNet algod hosts only. Primary is the submit host.
# Secondary is the already-mapped Nodely MainNet algod. Same org is
# acceptable for suggested-params (not independent confirm).
MAINNET_ALGOD_PARAMS_HOSTS = (
    netcfg.MAINNET.submit_host,  # mainnet-api.algonode.cloud
    "mainnet-api.4160.nodely.dev",
)
PRIMARY_PARAMS_HOST = MAINNET_ALGOD_PARAMS_HOSTS[0]


class ParamsError(ValueError):
    """Pinned MainNet suggested-params fetch failed closed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def params_url_for(host: str) -> str:
    """Hardcoded HTTPS + path. Host must be on the allowlist."""
    key = (host or "").strip().lower()
    if key not in MAINNET_ALGOD_PARAMS_HOSTS:
        raise ParamsError("algod host not allowlisted")
    return "https://%s%s" % (key, PARAMS_PATH)


def _pinned_https(url: str, host: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != host:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.path != PARAMS_PATH:
        return False
    if parsed.query or parsed.fragment:
        return False
    return True


def parse_algod_params(body: dict) -> dict:
    """Extract min-fee, fee-per-byte, last-round. Validate MainNet genesis."""
    if not isinstance(body, dict):
        raise ParamsError("invalid params body")
    try:
        last_round = int(body.get("last-round") if body.get("last-round") is not None else body.get("lastRound") or 0)
        min_fee = int(body.get("min-fee") if body.get("min-fee") is not None else body.get("minFee") or 0)
        fee_per_byte = int(body.get("fee") if body.get("fee") is not None else 0)
    except (TypeError, ValueError) as exc:
        raise ParamsError("invalid params integers") from exc
    if last_round < 1:
        raise ParamsError("missing lastRound")
    if min_fee < 1:
        raise ParamsError("invalid min-fee")
    if fee_per_byte < 0:
        raise ParamsError("invalid fee-per-byte")
    gid = str(body.get("genesis-id") or body.get("genesisID") or "").strip()
    gh = str(body.get("genesis-hash") or body.get("genesisHash") or "").strip()
    if gid != netcfg.MAINNET_GENESIS_ID or gh != netcfg.MAINNET_GENESIS_HASH:
        raise ParamsError("genesis mismatch")
    return {
        "minFee": min_fee,
        "fee": fee_per_byte,
        "feePerByte": fee_per_byte,
        "lastRound": last_round,
        "firstValid": last_round,
        "lastValid": last_round + 1000,
        "genesisID": netcfg.MAINNET_GENESIS_ID,
        "genesisHash": netcfg.MAINNET_GENESIS_HASH,
        "require_canonical": True,
        "flatFee": False,
        "params_host": "",
    }


def _get_params(host: str) -> dict:
    url = params_url_for(host)
    if not _pinned_https(url, host):
        raise ParamsError("params url not pinned")
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=PARAMS_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not isinstance(status, int) or status < 200 or status >= 300:
                raise ParamsError("params http status")
            raw = resp.read(PARAMS_MAX_BODY)
    except ParamsError:
        raise
    except urllib.error.HTTPError as exc:
        raise ParamsError("params http error") from exc
    except Exception as exc:
        raise ParamsError("params unreachable") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ParamsError("invalid params json") from exc
    out = parse_algod_params(body)
    out["params_host"] = host
    return out


def fetch_trusted_mainnet_params(*, fetch_fn=None, host: str | None = None) -> dict:
    """GET /v2/transactions/params from the hardcoded MainNet allowlist.

    fetch_fn is a test hook that must return a parsed params dict or
    raise. Production operator path never passes a URL or fee/fv/lv.
    """
    from live402 import fixtures

    if fixtures.fixture_mode() and fetch_fn is None and host is None:
        raise ParamsError("fixture mode never fetches live mainnet params")
    if fetch_fn is not None:
        if not callable(fetch_fn):
            raise ParamsError("invalid params hook")
        raw = fetch_fn()
        if isinstance(raw, dict) and raw.get("lastRound"):
            parsed = dict(raw)
            parsed.setdefault("genesisID", netcfg.MAINNET_GENESIS_ID)
            parsed.setdefault("genesisHash", netcfg.MAINNET_GENESIS_HASH)
            parsed["require_canonical"] = True
            parsed.setdefault("flatFee", False)
            return parsed
        if isinstance(raw, dict):
            return parse_algod_params(raw)
        raise ParamsError("invalid params hook result")
    if host is not None:
        return _get_params((host or "").strip().lower())
    errors = []
    for candidate in MAINNET_ALGOD_PARAMS_HOSTS:
        try:
            return _get_params(candidate)
        except ParamsError as exc:
            errors.append("%s:%s" % (candidate, exc))
    raise ParamsError("mainnet params unavailable")
