"""POST /route orchestration: verify → probe → settle."""

from __future__ import annotations

from live402 import facilitator, fixtures, payment, probe


def gate_open(headers) -> bool:
    """LOCAL_FREE tests-only. A payment header alone never opens the gate."""
    if fixtures.local_free():
        return True
    return False


def run_probe(body: dict) -> tuple[int, dict]:
    need = body.get("need")
    url = body.get("url")
    if need is not None and not isinstance(need, str):
        return 400, {"error": "need must be a string"}
    if url is not None and not isinstance(url, str):
        return 400, {"error": "url must be a string"}
    need = (need or "").strip()
    url = (url or "").strip()
    if not need and not url:
        return 400, {"error": "need or url is required"}

    if url:
        parsed_ok = url.lower().startswith("https://")
        if not parsed_ok:
            return 400, {"error": "url must be https"}
        item = fixtures.lookup_url(url)
        result = probe.probe_url(url, catalog_item=item)
        result = probe.attach_catalog_fields(result, item)
        result["need"] = need or None
        result["tried"] = 1
        result["source"] = "fixture" if fixtures.fixture_mode() else "url"
        result.setdefault("payTo", None)
        result.setdefault("traction", "unknown")
        if result.get("live"):
            return 200, result
        result["live"] = False
        return 503, result

    result = probe.route_need(need)
    result.setdefault("payTo", None)
    result.setdefault("traction", "unknown")
    if result.get("live"):
        return 200, result
    return 503, result


def _required_pair(resource_url: str, error: str | None = None) -> tuple[dict, dict]:
    required = payment.payment_required(resource_url)
    if error:
        required = dict(required)
        required["error"] = error
    extra = {"PAYMENT-REQUIRED": payment.payment_required_header(required)}
    return required, extra


def _bad_request(body: dict) -> tuple[int, dict] | None:
    """Body errors after a successful verify. Unpaid callers always get 402."""
    need = body.get("need")
    url = body.get("url")
    if need is not None and not isinstance(need, str):
        return 400, {"error": "need must be a string"}
    if url is not None and not isinstance(url, str):
        return 400, {"error": "url must be a string"}
    need_s = (need or "").strip() if isinstance(need, str) else ""
    url_s = (url or "").strip() if isinstance(url, str) else ""
    if not need_s and not url_s:
        return 400, {"error": "need or url is required"}
    if url_s and not url_s.lower().startswith("https://"):
        return 400, {"error": "url must be https"}
    return None


def handle_route(body: dict, headers, resource_url: str) -> tuple[int, dict, dict | None]:
    """Returns (status, json_body, extra_headers). Never probes before verify.

    Unpaid requests always 402 (empty JSON / missing need+url included) so
    CDP validate and bazaar crawlers can index. Body 400 only after verify
    succeeds, and we do not settle on 400.
    """
    if fixtures.local_free():
        code, result = run_probe(body if isinstance(body, dict) else {})
        return code, result, None

    parsed = payment.extract_payment_payload(headers)
    if not parsed:
        required, extra = _required_pair(resource_url)
        return 402, required, extra

    required_body = payment.payment_required(resource_url)
    accept = payment.match_accept(parsed, required_body)
    if not accept:
        required, extra = _required_pair(
            resource_url, "Payment does not match an advertised accept"
        )
        return 402, required, extra

    verify = facilitator.verify(parsed, accept)
    if not verify.ok:
        required, extra = _required_pair(
            resource_url, verify.error or "Payment verification failed"
        )
        return 402, required, extra

    # Paid: reject bad/empty body with 400 and skip settle.
    bad = _bad_request(body if isinstance(body, dict) else {})
    if bad:
        return bad[0], bad[1], None

    code, result = run_probe(body)
    if code == 400:
        return 400, result, None

    # Official authorization flow: verify → resource → settle → respond.
    # Product: settle after an honest miss too (they paid for the probe).
    settle = facilitator.settle(parsed, accept)
    extra: dict = {}
    if settle.body:
        extra["PAYMENT-RESPONSE"] = payment.payment_response_header(settle.body)
    if not settle.ok:
        required, pay_extra = _required_pair(
            resource_url, settle.error or "Payment settlement failed"
        )
        extra.update(pay_extra)
        return 402, required, extra
    return code, result, extra or None
