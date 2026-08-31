"""POST /route orchestration: verify → probe → settle."""

from __future__ import annotations

import sys
import time

from live402 import facilitator, fixtures, payment, probe, select
from live402 import policy as policy_mod


def gate_open(headers) -> bool:
    """LOCAL_FREE tests-only. A payment header alone never opens the gate."""
    if fixtures.local_free():
        return True
    return False


def run_probe(body: dict) -> tuple[int, dict]:
    need = body.get("need")
    url = body.get("url")
    if need is not None and not isinstance(need, str):
        return 400, {"error": "need must be a string", "miss_reason": "invalid_need", "live": False, "invocable": False}
    if url is not None and not isinstance(url, str):
        return 400, {"error": "url must be a string", "miss_reason": "invalid_need", "live": False, "invocable": False}
    need = (need or "").strip()
    url = (url or "").strip()
    if not need and not url:
        return 400, {"error": "need or url is required", "miss_reason": "invalid_need", "live": False, "invocable": False}

    deadline = time.monotonic() + probe.PROBE_BUDGET_SECONDS
    if url:
        parsed_ok = url.lower().startswith("https://")
        if not parsed_ok:
            return 400, {"error": "url must be https", "miss_reason": "invalid_need", "live": False, "invocable": False}
        item = fixtures.lookup_url(url)
        result = probe.probe_url(url, catalog_item=item, deadline=deadline)
        result = probe.attach_catalog_fields(result, item)
        try:
            from live402 import history as history_mod
            result = history_mod.attach_to_result(result)
        except Exception:
            if result.get("payTo_changed"):
                result["risk"] = ["payTo_changed"]
        if result.get("live"):
            selected = select.pick_selected_payment(result, "best", None)
            if selected:
                result["selected_payment"] = selected
                probe._align_target_with_selected(result, selected)
            if result.get("reputation") is None:
                try:
                    from live402 import reputation as reputation_mod

                    reputation_mod.attach(result)
                except Exception:
                    pass
        result["need"] = need or None
        result["tried"] = 1
        result["source"] = "fixture" if fixtures.fixture_mode() else "url"
        result["discovery_matches"] = 0
        result["candidates_discovered"] = 0
        result["candidates_considered"] = 1
        result["candidates_probed"] = 1
        result["probe_ceiling"] = 1
        result["probe_budget_exhausted"] = False
        result["candidate_evaluation_complete"] = True
        result["stop_reason"] = "winner_selected" if result.get("live") else "candidate_set_exhausted"
        result.setdefault("payTo", None)
        result.setdefault("traction", "unknown")
        policy_mod.attach_policy(result, body)
        if result.get("live"):
            return 200, result
        result["live"] = False
        result["invocable"] = False
        if result.get("miss_reason"):
            result["miss_reason"] = probe.public_miss_reason(result.get("miss_reason")) or result.get("miss_reason")
        return 503, result

    prefer = probe.normalize_prefer_network(body.get("prefer_network"))
    objective = select.parse_objective(body.get("objective"))
    constraints = policy_mod.merge_constraints(body)
    plan = probe.probe_plan(body)
    result = probe.route_need(
        need,
        deadline=deadline,
        prefer_network=prefer,
        objective=objective,
        constraints=constraints,
        search_depth=plan.get("search_depth"),
        max_candidates_to_probe=plan.get("max_candidates_to_probe"),
        probe_ceiling=plan.get("probe_ceiling"),
    )
    result.setdefault("payTo", None)
    result.setdefault("traction", "unknown")
    policy_mod.attach_policy(result, body)
    if result.get("live"):
        return 200, result
    return 503, result


def _algorand_sender(headers) -> str | None:
    raw = payment._header_get(headers, "Algorand-Sender", "X-Algorand-Sender")
    return raw or None


def _required_pair(resource_url: str, error: str | None = None, bazaar: dict | None = None, algorand_sender: str | None = None) -> tuple[dict, dict]:
    required = payment.payment_required(resource_url, bazaar=bazaar, algorand_sender=algorand_sender)
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
        return 400, {"error": "need must be a string", "miss_reason": "invalid_need", "live": False, "invocable": False}
    if url is not None and not isinstance(url, str):
        return 400, {"error": "url must be a string", "miss_reason": "invalid_need", "live": False, "invocable": False}
    need_s = (need or "").strip() if isinstance(need, str) else ""
    url_s = (url or "").strip() if isinstance(url, str) else ""
    if not need_s and not url_s:
        return 400, {"error": "need or url is required", "miss_reason": "invalid_need", "live": False, "invocable": False}
    if url_s and not url_s.lower().startswith("https://"):
        return 400, {"error": "url must be https", "miss_reason": "invalid_need", "live": False, "invocable": False}
    return None


def _log_settle(body: dict | None) -> None:
    """Log settlement, network, tx from PAYMENT-RESPONSE. No secrets."""
    if not isinstance(body, dict):
        return
    tx = body.get("transaction") or body.get("txHash") or body.get("tx") or body.get("hash")
    network = body.get("network")
    success = body.get("success")
    sys.stderr.write(
        "settle ok=%s network=%s tx=%s\n"
        % (success, network, tx)
    )


def handle_route(body: dict, headers, resource_url: str, bazaar: dict | None = None) -> tuple[int, dict, dict | None]:
    """Returns (status, json_body, extra_headers). Never probes before verify.

    Unpaid requests always 402 (empty JSON / missing need+url included) so
    CDP validate and bazaar crawlers can index. Body 400 only after verify
    succeeds, and we do not settle on 400.
    """
    if fixtures.local_free():
        code, result = run_probe(body if isinstance(body, dict) else {})
        return code, result, None

    parsed = payment.extract_payment_payload(headers)
    sender = _algorand_sender(headers)
    if not parsed:
        required, extra = _required_pair(resource_url, bazaar=bazaar, algorand_sender=sender)
        return 402, required, extra

    required_body = payment.payment_required(resource_url, bazaar=bazaar, algorand_sender=sender)
    accept = payment.match_accept(parsed, required_body)
    if not accept:
        required, extra = _required_pair(
            resource_url, "Payment does not match an advertised accept", bazaar=bazaar
        )
        return 402, required, extra

    verify = facilitator.verify(parsed, accept)
    if not verify.ok:
        required, extra = _required_pair(
            resource_url, verify.error or "Payment verification failed", bazaar=bazaar
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
            resource_url, settle.error or "Payment settlement failed", bazaar=bazaar
        )
        extra.update(pay_extra)
        return 402, required, extra
    _log_settle(settle.body)
    return code, result, extra or None
