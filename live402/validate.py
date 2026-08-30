"""Unpaid seller validator. Probe only. Never pay. Fail-closed SSRF."""

from __future__ import annotations

from live402 import catalog, fixtures, history, probe

READINESS = ("discovered", "payable", "invocable", "recently_verified")


def catalog_item_for(url: str) -> dict | None:
    """Lookup a catalog claim for claimed-vs-observed. Never recrawls."""
    raw = (url or "").strip()
    if not raw:
        return None
    item = fixtures.lookup_url(raw)
    if item:
        return item
    try:
        idx = catalog.peek_index()
    except Exception:
        idx = None
    if not isinstance(idx, dict):
        return None
    for row in idx.get("items") or []:
        if not isinstance(row, dict):
            continue
        if probe._resource_url(row) == raw:
            return row
    return None


def _ssrf_body(url: str) -> dict:
    probed_at = probe.now_iso()
    return {
        "url": url,
        "live": False,
        "payable": False,
        "invocable": False,
        "readiness": "discovered",
        "claimed": history._empty_claimed(),
        "observed": history._empty_observed(),
        "flags": [],
        "miss_reason": "ssrf",
        "verified_at": probed_at,
        "verified_seconds_ago": 0,
        "n_7d": 0,
    }


def _invalid_body(url: str | None, error: str) -> dict:
    return {
        "url": url,
        "error": error,
        "live": False,
        "payable": False,
        "invocable": False,
        "readiness": "discovered",
        "miss_reason": "invalid_need",
        "flags": [],
        "n_7d": 0,
    }


def _schema_present(result: dict) -> bool:
    observed = result.get("observed") if isinstance(result.get("observed"), dict) else {}
    if observed.get("schema_present") in (1, True):
        return True
    if result.get("invocable"):
        return True
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    schema = target.get("inputSchema")
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required")):
        return True
    if result.get("schema_source"):
        return True
    return False


def public_validate_body(result: dict) -> dict:
    """Slim seller-facing body. Omit healthy unless n_7d >= 10 observed."""
    if not isinstance(result, dict):
        return _invalid_body(None, "url is required")
    url = result.get("url")
    pay_to = result.get("payTo")
    live = bool(result.get("live"))
    payable = bool(live and pay_to)
    observed = result.get("observed") if isinstance(result.get("observed"), dict) else history._empty_observed()
    claimed = result.get("claimed") if isinstance(result.get("claimed"), dict) else history._empty_claimed()
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    try:
        n_7d = int(hist.get("n_7d") or 0)
    except (TypeError, ValueError):
        n_7d = 0
    readiness = result.get("readiness")
    if readiness not in READINESS:
        readiness = history.compute_readiness(result, n_7d)
    flags: list[str] = []
    if result.get("payTo_changed") or "payTo_changed" in (result.get("risk") or []):
        flags.append("payTo_changed")
    if not _schema_present(result):
        flags.append("missing schema")
    status = observed.get("http_status")
    if status is None:
        status = result.get("status")
    has_402 = bool(result.get("has_402_challenge") or status == 402)
    obs_pay = observed.get("payTo") or pay_to
    if has_402 and not obs_pay:
        flags.append("402 without payTo")
    claimed_out = {
        "payTo": claimed.get("payTo"),
        "amount": claimed.get("amount"),
        "schema_present": claimed.get("schema_present"),
    }
    observed_out = {
        "payTo": observed.get("payTo"),
        "amount": observed.get("amount"),
        "schema_present": observed.get("schema_present"),
        "http_status": observed.get("http_status") if observed.get("http_status") is not None else result.get("status"),
        "latency_ms": observed.get("latency_ms") if observed.get("latency_ms") is not None else result.get("latency_ms"),
    }
    out = {
        "url": url,
        "live": live,
        "payable": payable,
        "invocable": bool(result.get("invocable")),
        "readiness": readiness,
        "claimed": claimed_out,
        "observed": observed_out,
        "flags": flags,
        "verified_at": result.get("verified_at") or result.get("probed_at"),
        "verified_seconds_ago": result.get("verified_seconds_ago", 0),
        "n_7d": n_7d,
    }
    if result.get("miss_reason") and not live:
        out["miss_reason"] = probe.public_miss_reason(result.get("miss_reason")) or result.get("miss_reason")
    if n_7d >= history.MIN_HEALTHY_N:
        success = hist.get("success_7d")
        out["healthy"] = bool(success is not None and success > 0)
    return out


def _not_listed_body(url: str) -> dict:
    probed_at = probe.now_iso()
    return {
        "url": url,
        "live": False,
        "payable": False,
        "invocable": False,
        "readiness": "discovered",
        "claimed": history._empty_claimed(),
        "observed": history._empty_observed(),
        "flags": [],
        "miss_reason": "no_candidates",
        "verified_at": probed_at,
        "verified_seconds_ago": 0,
        "n_7d": 0,
    }


def validate_url(url: str) -> tuple[int, dict]:
    """Probe a seller URL already in the catalog or fixture. Unpaid. Never a /route payment bypass.

    Gate: no probe._one_request unless the URL is in catalog or fixture.
    Unpaid validate never writes 402signal_observed.
    """
    raw = (url or "").strip() if isinstance(url, str) else ""
    if not raw:
        return 400, _invalid_body(None, "url is required")
    if not raw.lower().startswith("https://"):
        return 400, _invalid_body(raw, "url must be https")
    if probe.skip_candidate_url(raw):
        return 200, _ssrf_body(raw)
    item = catalog_item_for(raw)
    if item is None:
        return 200, _not_listed_body(raw)
    if not fixtures.fixture_mode() and not probe.safe_target(raw):
        return 200, _ssrf_body(raw)
    result = probe.probe_url(raw, catalog_item=item, record=False)
    return 200, public_validate_body(result)
