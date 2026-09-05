"""POST /route orchestration: verify → probe → settle only a valid winner."""

from __future__ import annotations

import sys
import time

from live402 import deadline as deadline_mod
from live402 import facilitator, fixtures, payment, probe, replay, reqctx, select
from live402 import policy as policy_mod
from live402 import lab_traffic


def _preserve_observed_facts(result: dict) -> None:
    """Keep current-observation facts when a miss zeros decision fields."""
    if not isinstance(result, dict):
        return
    obs = result.get("observed") if isinstance(result.get("observed"), dict) else {}
    obs = dict(obs)
    if obs.get("live") is None:
        obs["live"] = bool(result.get("live"))
    if obs.get("payable") is None and result.get("payable") is not None:
        obs["payable"] = result.get("payable")
    if obs.get("invocable") is None and result.get("invocable") is not None:
        obs["invocable"] = result.get("invocable")
    if obs.get("http_status") is None and result.get("status") is not None:
        obs["http_status"] = result.get("status")
    if obs.get("latency_ms") is None and result.get("latency_ms") is not None:
        obs["latency_ms"] = result.get("latency_ms")
    result["observed"] = obs


def gate_open(headers) -> bool:
    """LOCAL_FREE tests-only. A payment header alone never opens the gate."""
    if fixtures.local_free():
        return True
    return False


def _invalid_need(error: str) -> tuple[int, dict]:
    return 400, {"error": error, "miss_reason": "invalid_need", "live": False, "invocable": False}


def _lookup_claimed(url: str) -> dict | None:
    """Claimed invocation metadata only. Fixtures in fixture mode; else exact local catalog."""
    if fixtures.fixture_mode():
        return fixtures.lookup_url(url)
    try:
        from live402 import catalog as catalog_mod

        return catalog_mod.claimed_item_for_url(url)
    except Exception:
        return None


def _direct_url_result(body: dict, url: str, need: str, deadline: float) -> tuple[int, dict]:
    """Same constraint engine as need-routing. Catalog item is claimed metadata only."""
    objective = select.parse_objective(body.get("objective"))
    constraints = policy_mod.merge_constraints(body)
    item = _lookup_claimed(url)
    if not probe.direct_url_allowed(url, item):
        result = {
            "url": url,
            "need": need or None,
            "live": False,
            "invocable": False,
            "payable": False,
            "selected_payment": None,
            "miss_reason": "ssrf",
            "stop_reason": "candidate_set_exhausted",
            "tried": 0,
            "source": "url",
            "discovery_matches": 0,
            "candidates_discovered": 0,
            "candidates_considered": 1,
            "candidates_probed": 0,
            "probe_ceiling": 1,
            "probe_budget_exhausted": False,
            "candidate_evaluation_complete": True,
            "evaluation_complete": True,
            "discovered_count": 0,
            "probed_count": 0,
            "unprobed_count": 0,
            "payTo": None,
            "traction": "unknown",
            "objective": objective,
        }
        policy_mod.attach_policy(result, body)
        return 503, result
    result = probe.probe_url(url, catalog_item=item, deadline=deadline, record=False)
    result = probe.attach_catalog_fields(result, item)
    if not lab_traffic.is_lab_url(url):
        try:
            from live402 import history as history_mod

            bid = result.get("batch_id")
            metas = history_mod.persist_route_batch(bid, [result]) if bid else {}
            meta = metas.get(url) if isinstance(metas, dict) else None
            if meta and meta.get("payTo_pending"):
                result["payTo_pending"] = True
                result["payTo_changed"] = True
                result.setdefault("risk", ["payTo_changed"])
            elif meta and meta.get("payTo_flipped"):
                result["payTo_changed"] = True
                result.setdefault("risk", ["payTo_changed"])
        except Exception:
            pass
        try:
            from live402 import history as history_mod
            result = history_mod.attach_to_result(result)
        except Exception:
            if result.get("payTo_changed"):
                result["risk"] = ["payTo_changed"]
    result["need"] = need or None
    result["objective"] = objective
    result["tried"] = 1
    result["source"] = "fixture" if fixtures.fixture_mode() else "url"
    result["discovery_matches"] = 0
    result["candidates_discovered"] = 0
    result["candidates_considered"] = 1
    result["candidates_probed"] = 1
    result["probe_ceiling"] = 1
    result["probe_budget_exhausted"] = False
    result["candidate_evaluation_complete"] = True
    result["evaluation_complete"] = True
    result["discovered_count"] = 0
    result["probed_count"] = 1
    result["unprobed_count"] = 0
    result.setdefault("payTo", None)
    result.setdefault("traction", "unknown")
    policy_mod.attach_policy(result, body)

    selected = None
    if (
        result.get("live")
        and select.passes_constraints(result, constraints)
        and select._payto_selectable(result, constraints)
    ):
        selected = select.pick_selected_payment(result, objective, constraints)
    if selected:
        result["selected_payment"] = selected
        probe._align_target_with_selected(result, selected)
        if result.get("reputation") is None and not lab_traffic.is_lab_url(url):
            try:
                from live402 import reputation as reputation_mod

                reputation_mod.attach(result)
            except Exception:
                pass
        result["stop_reason"] = "winner_selected"
        return 200, result

    if result.get("live"):
        result["miss_reason"] = "constraints_unmet"
        result["stop_reason"] = "constraints_unmet"
        result["challenge_observed"] = True if result.get("challenge_observed") is None else result.get("challenge_observed")
    else:
        result["stop_reason"] = "candidate_set_exhausted"
        if result.get("miss_reason"):
            result["miss_reason"] = probe.public_miss_reason(result.get("miss_reason")) or result.get("miss_reason")
    _preserve_observed_facts(result)
    result["live"] = False
    result["invocable"] = False
    result["payable"] = False
    result["selected_payment"] = None
    return 503, result


def run_probe(body: dict, deadline: float | None = None) -> tuple[int, dict]:
    need = body.get("need")
    url = body.get("url")
    if need is not None and not isinstance(need, str):
        return _invalid_need("need must be a string")
    if url is not None and not isinstance(url, str):
        return _invalid_need("url must be a string")
    need = (need or "").strip()
    url = (url or "").strip()
    if not need and not url:
        return _invalid_need("need or url is required")
    try:
        select.validate_explicit_constraints(body if isinstance(body, dict) else {})
    except select.ConstraintError as exc:
        return _invalid_need(str(exc))

    if deadline is None:
        deadline = time.monotonic() + probe.PROBE_BUDGET_SECONDS
    if url:
        if not url.lower().startswith("https://"):
            return _invalid_need("url must be https")
        return _direct_url_result(body, url, need, deadline)

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
    if result.get("live") and select.http200_winner_ok(result, objective, constraints):
        return 200, result
    if result.get("live"):
        result["miss_reason"] = result.get("miss_reason") or "constraints_unmet"
        if result.get("miss_reason") == "no_input_schema":
            result["miss_reason"] = "constraints_unmet"
        result["stop_reason"] = "constraints_unmet"
        unmet = select.collect_unmet_constraints([result], constraints)
        if unmet:
            result["unmet_constraints"] = unmet
        _preserve_observed_facts(result)
        result["live"] = False
        result["invocable"] = False
        result["payable"] = False
        result["selected_payment"] = None
    return 503, result


def _algorand_sender(headers) -> str | None:
    raw = payment._header_get(headers, "Algorand-Sender", "X-Algorand-Sender")
    return raw or None


def _required_pair(resource_url: str, error: str | None = None, bazaar: dict | None = None, algorand_sender: str | None = None) -> tuple[dict, dict]:
    required = payment.payment_required(resource_url, bazaar=bazaar, algorand_sender=algorand_sender)
    if error:
        required = dict(required)
        required["error"] = error
    lab_traffic.advertise(required)
    extra = {"PAYMENT-REQUIRED": payment.payment_required_header(required)}
    return required, extra


def _bad_request(body: dict) -> tuple[int, dict] | None:
    """Body errors after a successful verify. Unpaid callers always get 402."""
    if "lab_test" in body and (body.get("lab_test") != lab_traffic.PROTOCOL
                                  or not lab_traffic.is_lab_url(body.get("url"))):
        return 400, {"error": "lab target is not configured", "live": False}
    if lab_traffic.is_lab_url(body.get("url")) and _require_transparency(body):
        return 400, {"error": "lab tests exclude production transparency", "live": False}
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


def _log_settle(ok: bool, rail: str) -> None:
    """Coarse settlement log. No txid, no payment payload."""
    rid = reqctx.request_id.get()
    sys.stderr.write(
        "settlement_success=%s rail=%s request_id=%s\n"
        % ("true" if ok else "false", rail or "unknown", rid or "-")
    )


def _log_settle_skipped(rail: str) -> None:
    """Coarse success-only decision log. No payment material."""
    rid = reqctx.request_id.get()
    sys.stderr.write(
        "settlement_skipped=true reason=no_billable_winner rail=%s request_id=%s\n"
        % (rail or "unknown", rid or "-")
    )


def _billing(
    rail: str,
    *,
    settlement_attempted: bool | None,
    settled: bool | None,
    settlement_state: str,
) -> dict:
    return {
        "model": payment.ROUTING_BILLING_MODEL,
        "condition": payment.ROUTING_SETTLEMENT_CONDITION,
        "asset": "USDC",
        "amount_atomic": payment.AMOUNT_ATOMIC,
        "display_amount": payment.AMOUNT_USD,
        "rail": rail or "unknown",
        "settlement_attempted": settlement_attempted,
        "settled": settled,
        "settlement_state": settlement_state,
    }


def _unknown_outcome(rail: str, *, attempted: bool | None) -> tuple[int, dict, None]:
    return 503, {
        "error": "Routing authorization outcome unknown; do not retry this authorization",
        "live": False,
        "payable": False,
        "invocable": False,
        "selected_payment": None,
        "miss_reason": "settlement_unknown",
        "billing": _billing(
            rail,
            settlement_attempted=attempted,
            settled=None,
            settlement_state="unknown",
        ),
    }, None


def _billable_winner(body: dict, code: int, result: dict) -> bool:
    """Independent final settlement gate over current observed wire evidence."""
    if code != 200 or not isinstance(result, dict):
        return False
    if result.get("live") is not True or result.get("payable") is not True:
        return False
    url = result.get("url")
    if type(url) is not str or not url.strip().lower().startswith("https://"):
        return False
    # 402Signal defines a live x402 route as a current HTTP 402 challenge;
    # a reachable HTTP 200 must never become billable through field confusion.
    if type(result.get("status")) is not int or result.get("status") != 402:
        return False
    try:
        objective = select.parse_objective(body.get("objective"))
        constraints = policy_mod.merge_constraints(body)
        if not select.http200_winner_ok(result, objective, constraints):
            return False
        if not select._payto_selectable(result, constraints):
            return False
        selected = result.get("selected_payment")
        if not payment.selected_payment_matches_current_envelope(selected, result):
            return False
        if not select.selected_payment_passes_constraints(result, selected, constraints):
            return False
        return payment.payto_equal(
            result.get("payTo"), selected.get("payTo"), selected.get("rail")
        )
    except Exception:
        return False


def _downgrade_unbillable_result(result: dict) -> dict:
    """Turn a purported malformed winner into a typed fail-closed miss."""
    out = dict(result) if isinstance(result, dict) else {}
    out["error"] = "route result failed billable winner validation"
    out["miss_reason"] = out.get("miss_reason") or "constraints_unmet"
    out["stop_reason"] = "constraints_unmet"
    _preserve_observed_facts(out)
    out["live"] = False
    out["payable"] = False
    out["invocable"] = False
    out["selected_payment"] = None
    return out


def _require_transparency(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("require_route_binding") is True:
        return True
    raw = body.get("require_transparency")
    return raw is True or (isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes"})


def _transparency_ok(result: dict) -> bool:
    """True only for a durable signed leaf (pending / checkpoint_signed + checkpoint).

    SEC-ROUTER-004 / A-14: logged_uncheckpointed and unavailable are never
    success when require_transparency is set.
    """
    tr = ((result.get("pq_trust") or {}).get("transparency") or {}) if isinstance(result, dict) else {}
    status = str(tr.get("status") or "")
    state = str(tr.get("state") or "")
    if status in {"logged_uncheckpointed", "unavailable"}:
        return False
    if status == "pending" or state == "checkpoint_signed":
        return bool(tr.get("receipt") and (tr.get("receipt") or {}).get("checkpoint"))
    return False


def _attach_pq_trust(code: int, result: dict, body: dict) -> dict:
    """Optional transparency receipt. Settlement is not atomic with log append.

    SEC-ROUTER-004 / A-14: a settled hit (200) does not require a durable
    signed leaf unless require_transparency is set. Free misses never call this.
    Append failure is best-effort (logged_uncheckpointed or unavailable).
    """
    if not isinstance(result, dict):
        return result
    if code not in (200, 503):
        return result
    try:
        from live402.pq import receipt as pq_receipt

        return pq_receipt.attach_to_route(result, body if isinstance(body, dict) else {})
    except Exception:
        result.setdefault("payment_authorization", {})
        if isinstance(result.get("payment_authorization"), dict):
            result["payment_authorization"]["pq_native"] = False
        result["pq_trust"] = {
            "transparency": {
                "status": "unavailable",
                "state": "unavailable",
                "log_origin": "402signal.com/pq/log",
            }
        }
        return result


def _paid_execute(
    body: dict,
    parsed: dict,
    accept: dict,
    resource_url: str,
    bazaar: dict | None,
    paid_deadline: float,
) -> tuple[int, dict, dict | None]:
    verify_t = deadline_mod.verify_timeout(paid_deadline)
    if verify_t <= 0:
        required, extra = _required_pair(
            resource_url, "Payment verification failed", bazaar=bazaar
        )
        required["billing"] = _billing(
            payment.rail_of_accept(accept),
            settlement_attempted=False,
            settled=False,
            settlement_state="rejected",
        )
        return 402, required, extra

    verify = facilitator.verify(parsed, accept, timeout=verify_t)
    if not verify.ok:
        required, extra = _required_pair(
            resource_url, "Payment verification failed", bazaar=bazaar
        )
        required["billing"] = _billing(
            payment.rail_of_accept(accept),
            settlement_attempted=False,
            settled=False,
            settlement_state="rejected",
        )
        return 402, required, extra

    # Paid: reject bad/empty body with 400 and skip settle.
    bad = _bad_request(body if isinstance(body, dict) else {})
    if bad:
        return bad[0], bad[1], None

    probe_until = deadline_mod.probe_deadline(paid_deadline)
    code, result = run_probe(body, deadline=probe_until)
    if code == 400:
        return 400, result, None

    rail = payment.rail_of_accept(accept)
    is_lab = lab_traffic.is_lab_url(body.get("url"))
    if is_lab and isinstance(result, dict):
        result["lab_testing"] = lab_traffic.classification()
    if not _billable_winner(body, code, result):
        if code == 200 or (
            isinstance(result, dict)
            and (
                result.get("live") is True
                or result.get("payable") is True
                or result.get("selected_payment") is not None
            )
        ):
            result = _downgrade_unbillable_result(result)
            code = 503
        if not isinstance(result, dict):
            result = _downgrade_unbillable_result({})
            code = 503
        result["billing"] = _billing(
            rail,
            settlement_attempted=False,
            settled=False,
            settlement_state="not_attempted",
        )
        _log_settle_skipped(rail)
        # Free misses remain tentative history and create no PQ route leaf.
        return code, result, None

    if body.get("require_route_binding") is True:
        from live402 import route_binding
        from live402.pq import route_v4

        try:
            result["decision_binding"] = route_binding.build(result, body)
            # Prevalidate full evidence before an economic action.
            route_v4.evidence_from_route(result, body)
        except (ValueError, TypeError, KeyError):
            result = _downgrade_unbillable_result(result)
            result.pop("decision_binding", None)
            result["binding_error"] = "route_binding_unavailable"
            result["billing"] = _billing(rail, settlement_attempted=False, settled=False,
                                         settlement_state="not_attempted")
            _log_settle_skipped(rail)
            return 503, result, None

    settle_t = deadline_mod.settle_timeout(paid_deadline)
    if settle_t <= 0:
        required, extra = _required_pair(
            resource_url, "Payment settlement not attempted", bazaar=bazaar
        )
        required["billing"] = _billing(
            rail,
            settlement_attempted=False,
            settled=False,
            settlement_state="rejected",
        )
        _log_settle_skipped(rail)
        return 402, required, extra
    settle = facilitator.settle(parsed, accept, timeout=settle_t)
    extra: dict = {}
    if not settle.ok:
        if settle.ambiguous:
            _log_settle(False, rail)
            return _unknown_outcome(rail, attempted=True)
        required, pay_extra = _required_pair(
            resource_url, "Payment settlement failed", bazaar=bazaar
        )
        required["billing"] = _billing(
            rail,
            settlement_attempted=True,
            settled=False,
            settlement_state="rejected",
        )
        extra.update(pay_extra)
        _log_settle(False, rail)
        return 402, required, extra
    safe_receipt = payment.sanitize_settlement_receipt(settle.body, rail=rail)
    if safe_receipt is None:
        _log_settle(False, rail)
        return _unknown_outcome(rail, attempted=True)
    extra["PAYMENT-RESPONSE"] = payment.payment_response_header(safe_receipt)
    _log_settle(True, rail)
    result["billing"] = _billing(
        rail,
        settlement_attempted=True,
        settled=True,
        settlement_state="settled",
    )
    if is_lab:
        result.pop("binding_observation", None)
        result["pq_trust"] = {"transparency": {"status": "excluded_self_test", "state": "not_recorded"}}
        return code, result, extra
    try:
        from live402 import history as history_mod

        history_mod.mark_batch_settled(result.get("batch_id") if isinstance(result, dict) else None)
    except Exception:
        pass
    result.pop("binding_observation", None)
    attached = _attach_pq_trust(code, result, body if isinstance(body, dict) else {})
    if _require_transparency(body) and not _transparency_ok(attached):
        return 503, {
            "error": "transparency receipt unavailable",
            "live": False,
            "invocable": False,
            "miss_reason": attached.get("miss_reason") if isinstance(attached, dict) else None,
            "billing": attached.get("billing") if isinstance(attached, dict) else None,
            "pq_trust": attached.get("pq_trust") if isinstance(attached, dict) else None,
        }, extra or None
    return code, attached, extra or None


def handle_route(body: dict, headers, resource_url: str, bazaar: dict | None = None) -> tuple[int, dict, dict | None]:
    """Returns (status, json_body, extra_headers). Never probes before verify.

    Unpaid requests always 402 (empty JSON / missing need+url included) so
    CDP validate and bazaar crawlers can index. Body 400 only after verify
    succeeds, and we do not settle on 400.
    """
    if fixtures.local_free():
        code, result = run_probe(body if isinstance(body, dict) else {})
        if lab_traffic.is_lab_url((body or {}).get("url")):
            result["lab_testing"] = lab_traffic.classification()
            return code, result, None
        try:
            from live402 import history as history_mod

            history_mod.mark_batch_settled(result.get("batch_id") if isinstance(result, dict) else None)
        except Exception:
            pass
        attached = _attach_pq_trust(code, result, body if isinstance(body, dict) else {})
        if _require_transparency(body) and not _transparency_ok(attached):
            return 503, {
                "error": "transparency receipt unavailable",
                "live": False,
                "invocable": False,
                "miss_reason": attached.get("miss_reason") if isinstance(attached, dict) else None,
                "pq_trust": attached.get("pq_trust") if isinstance(attached, dict) else None,
            }, None
        return code, attached, None

    parsed = payment.extract_payment_payload(headers)
    sender = _algorand_sender(headers)
    if not parsed:
        required, extra = _required_pair(resource_url, bazaar=bazaar, algorand_sender=sender)
        return 402, required, extra

    required_body = payment.payment_required(resource_url, bazaar=bazaar, algorand_sender=sender)
    accept = payment.match_accept(parsed, required_body)
    if not accept:
        required, extra = _required_pair(
            resource_url, payment.inbound_match_error(parsed), bazaar=bazaar
        )
        return 402, required, extra

    paid_deadline = deadline_mod.payment_deadline(accept)
    try:
        fp = replay.canonical_fingerprint(parsed, accept)
        legacy_fp = replay.legacy_fingerprint(parsed, accept)
    except (TypeError, ValueError):
        required, extra = _required_pair(
            resource_url, "Payment verification failed", bazaar=bazaar
        )
        return 402, required, extra
    kind, token = replay.begin(fp, legacy_fp=legacy_fp, scope=resource_url)
    if kind == "cached" and isinstance(token, tuple) and len(token) == 3:
        return token[0], token[1], token[2]
    if kind == "reject":
        return _unknown_outcome(payment.rail_of_accept(accept), attempted=None)
    if kind == "wait":
        waited = replay.wait_result(token, paid_deadline)
        if isinstance(waited, tuple) and len(waited) == 3:
            return waited[0], waited[1], waited[2]
        return _unknown_outcome(payment.rail_of_accept(accept), attempted=None)

    cache = False
    try:
        out = _paid_execute(body, parsed, accept, resource_url, bazaar, paid_deadline)
        # Cache terminal settled, not-settled, and rejected outcomes. A 400 may retry.
        cache = out[0] != 400
        replay.finish(fp, out, cache=cache)
        return out
    except Exception:
        out = _unknown_outcome(payment.rail_of_accept(accept), attempted=None)
        replay.finish(fp, out, cache=True)
        return out
