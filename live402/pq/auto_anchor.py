"""Default-off MainNet Falcon anchor controller.

The controller never runs on a request path. It freezes one checkpoint,
persists sign intent, obtains one authenticated pq-anchor/3 SignedTxn, then
atomically reserves fee budgets and SEND_ATTEMPTED before one network POST.
Every later tick polls only the locally-derived expected txid.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from live402.pq import ANCHOR_SLA_LEAVES, ANCHOR_SLA_SECONDS, ORIGIN_MAINNET
from live402.pq import algo_anchor
from live402.pq import canary
from live402.pq import log_identity
from live402.pq import mainnet_params
from live402.pq import network as netcfg
from live402.pq import ops_state
from live402.pq import store

_lock = threading.Lock()
_log = logging.getLogger("live402.pq.auto_anchor")
_event_lock = threading.Lock()
_last_event_at: dict[tuple[str, int], float] = {}
_EVENT_REPEAT_S = 15 * 60
_retry_lock = threading.Lock()
_retry_after: dict[tuple[str, str], int] = {}
_PREPARE_RETRY_S = 60
_CONFIRM_RETRY_S = 10


class AutoAnchorError(RuntimeError):
    pass


def _safe_event(code: str, *, tree_size: int = 0) -> None:
    token = str(code or "auto_error").strip()[:80]
    key = (token, int(tree_size or 0))
    now = time.monotonic()
    with _event_lock:
        last = float(_last_event_at.get(key) or 0)
        if last and now - last < _EVENT_REPEAT_S:
            return
        _last_event_at[key] = now
    ops_state.record_error("auto_" + token)
    _log.warning("pq_auto_anchor code=%s tree_size=%d", token, int(tree_size or 0))


def _retry_key(scope: str) -> tuple[str, str]:
    return (store.db_path(), str(scope))


def _retry_ready(scope: str, *, now: int) -> bool:
    with _retry_lock:
        return int(now) >= int(_retry_after.get(_retry_key(scope)) or 0)


def _defer_retry(scope: str, *, now: int, delay: int) -> None:
    with _retry_lock:
        _retry_after[_retry_key(scope)] = int(now) + int(delay)


def _clear_retry(scope: str) -> None:
    with _retry_lock:
        _retry_after.pop(_retry_key(scope), None)


def _track_resume_result(result: dict | None, *, tree_size: int, now: int):
    status = str(
        (result or {}).get("status") or (result or {}).get("send_state") or ""
    )
    scope = "poll:%d" % int(tree_size)
    if status in {canary.STATE_SEND_ATTEMPTED, canary.STATE_SUBMITTED}:
        _clear_retry("resume:%d" % int(tree_size))
        _defer_retry(scope, now=now, delay=_CONFIRM_RETRY_S)
    elif status == canary.STATE_CONFIRMED:
        _clear_retry("resume:%d" % int(tree_size))
        _clear_retry(scope)
    elif status == "PENDING":
        _defer_retry("resume:%d" % int(tree_size), now=now, delay=_CONFIRM_RETRY_S)
    elif status in {"BUDGET_WAIT", "PRE_POST_WAIT"}:
        _defer_retry("resume:%d" % int(tree_size), now=now, delay=_PREPARE_RETRY_S)
    return result


def _runtime_allowed() -> bool:
    try:
        return bool(
            log_identity.is_production_runtime()
            and log_identity.configured_network() == algo_anchor.MAINNET_NAME
            and (store.origin() or log_identity.configured_origin()) == ORIGIN_MAINNET
        )
    except Exception:
        return False


def _configuration_gate() -> None:
    if not _runtime_allowed():
        raise AutoAnchorError("not_production_mainnet")
    if not algo_anchor.automatic_mainnet_enabled():
        raise AutoAnchorError("disabled")
    if algo_anchor.automatic_mainnet_killed():
        raise AutoAnchorError("killed")
    if not algo_anchor.mainnet_broadcast_requested():
        raise AutoAnchorError("broadcast_gate_off")
    if algo_anchor.mainnet_canary_requested():
        raise AutoAnchorError("manual_canary_conflict")
    if not algo_anchor.mainnet_signer_configured():
        raise AutoAnchorError("signer_not_configured")
    try:
        confirmation = netcfg.confirmation_status(algo_anchor.MAINNET_NAME)
    except Exception as exc:
        raise AutoAnchorError("confirm_provider_invalid") from exc
    if not confirmation.get("confirm_provider_known"):
        raise AutoAnchorError("confirm_provider_not_selected")
    if not confirmation.get("confirm_org_independent"):
        raise AutoAnchorError("confirm_provider_not_independent")
    if not confirmation.get("confirm_credentials_configured"):
        raise AutoAnchorError("confirm_credentials_missing")
    if not confirmation.get("confirm_falcon_compatible"):
        raise AutoAnchorError("confirm_provider_falcon_unproven")


def _confirmation_path_verified(*, fetch_fn=None) -> bool:
    """Re-verify the latest confirmed Falcon anchor through the live provider."""
    confirmed = store.last_confirmed_checkpoint()
    txid = str(confirmed.get("txid") or "")
    size = int(confirmed.get("size") or 0)
    root = str(confirmed.get("root") or "")
    if size < 1 or not txid or len(root) != 64:
        return False
    fetched = algo_anchor.fetch_confirmed_txn(
        txid, network=algo_anchor.MAINNET_NAME, fetch_fn=fetch_fn
    )
    if not fetched:
        return False
    try:
        decoded = algo_anchor.decode_chain_txn(fetched)
        verified = algo_anchor.verify_fetched_anchor(
            decoded,
            expected_origin=str(confirmed.get("origin") or ORIGIN_MAINNET),
            expected_size=size,
            expected_root=bytes.fromhex(root),
            expected_address=algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME),
            expected_txid=txid,
            expected_network=algo_anchor.MAINNET_NAME,
        )
    except (ValueError, algo_anchor.AnchorError):
        return False
    if str(verified.get("txid") or "") != txid:
        return False
    try:
        provider = netcfg.configured_confirm_provider()
        if provider is None or not netcfg.CONFIRM_FALCON_COMPATIBLE.get(provider.name):
            return False
        store.save_confirmation_provider_proof(
            provider=provider.name,
            host=provider.host,
            tree_size=size,
            root=root,
            txid=txid,
            verified_at=int(time.time()),
        )
    except (netcfg.UnknownNetwork, store.StoreError, TypeError, ValueError):
        return False
    return True


def _identity_from_job(job: dict) -> dict:
    return {
        "tree_size": int(job.get("tree_size") or 0),
        "origin": str(job.get("origin") or ""),
        "root": str(job.get("root") or ""),
        "checkpoint": str(job.get("checkpoint") or ""),
    }


def _job_matches_authorized(job: dict, auth: dict) -> bool:
    return bool(
        job
        and auth
        and int(job.get("tree_size") or 0)
        == int(auth.get("tree_size") or auth.get("size") or 0)
        and str(job.get("origin") or "") == str(auth.get("origin") or "")
        and str(job.get("root") or "") == str(auth.get("root") or "")
        and str(job.get("checkpoint") or "") == str(auth.get("checkpoint") or "")
        and str(job.get("request_id") or "") == str(auth.get("request_id") or "")
    )


def _authorize_job(job: dict, *, sign_fn=None, host=None, port=None, now: int) -> dict:
    job = store.record_automation_sign_attempt(int(job["tree_size"]), now=now)
    auth = canary.authorize(
        now=int(job["authorize_at"]),
        request_id=str(job["request_id"]),
        params=dict(job["params"]),
        sign_fn=sign_fn,
        host=host,
        port=port,
        _identity=_identity_from_job(job),
        _capability=canary._AUTO_SEND_CAPABILITY,
    )
    if not _job_matches_authorized(job, auth):
        raise canary.CanarySecurityError("automatic authorization mismatch")
    store.set_automation_job_status(int(job["tree_size"]), "AUTHORIZED", now=now)
    return auth


def _send(
    auth: dict,
    *,
    now: int,
    send_fn=None,
    fetch_fn=None,
    balance_fetch_fn=None,
) -> dict:
    return canary.send_automatic_durable(
        auth,
        _capability=canary._AUTO_SEND_CAPABILITY,
        send_fn=send_fn,
        fetch_fn=fetch_fn,
        balance_fetch_fn=balance_fetch_fn,
        now=now,
    )


def _budget_block(fee: int, *, now: int) -> str:
    """Advisory precheck; the durable reservation repeats this atomically."""
    amount = int(fee or 0)
    if amount < algo_anchor.MIN_FEE or amount > canary.AUTO_MAX_FEE:
        return "fee_cap"
    hour, day, month = canary._budget_windows(now)
    used = store.automatic_budget_usage(
        hour_start=hour, day_start=day, month_start=month
    )
    if int(used.get("hourly_count") or 0) >= canary.AUTO_HOURLY_ANCHOR_MAX:
        return "hourly_anchor_cap"
    if int(used.get("daily_fee") or 0) + amount > canary.AUTO_DAILY_FEE_MAX:
        return "daily_fee_cap"
    if int(used.get("monthly_fee") or 0) + amount > canary.AUTO_MONTHLY_FEE_MAX:
        return "monthly_fee_cap"
    return ""


def _eligible(*, delta: int, unanchored_since: int, now: int) -> bool:
    if int(delta) < 1:
        return False
    if int(delta) >= ANCHOR_SLA_LEAVES:
        return True
    since = int(unanchored_since or 0)
    return bool(since >= 1 and int(now) - since >= ANCHOR_SLA_SECONDS)


def _halt(job: dict, code: str, *, now: int) -> None:
    try:
        store.set_automation_job_status(
            int(job.get("tree_size") or 0), "HALTED", now=now, error=code
        )
    finally:
        _safe_event(code, tree_size=int(job.get("tree_size") or 0))


def _resume_job(
    job: dict,
    *,
    now: int,
    sign_fn=None,
    send_fn=None,
    fetch_fn=None,
    balance_fetch_fn=None,
    host=None,
    port=None,
    params_fetch_fn=None,
) -> dict | None:
    size = int(job.get("tree_size") or 0)
    status = str(job.get("status") or "")
    if status == "HALTED":
        return {"status": "HALTED", "tree_size": size}
    auth = store.authorized_at(size)
    if status == "PENDING":
        if now - int(job.get("authorize_at") or 0) > algo_anchor.SNAPSHOT_MAX_AGE_S:
            _halt(job, "sign_intent_expired", now=now)
            return {"status": "HALTED", "tree_size": size}
        try:
            auth = _authorize_job(job, sign_fn=sign_fn, host=host, port=port, now=now)
            job = store.automation_job_at(size) or job
        except Exception:
            latest = store.automation_job_at(size) or job
            if int(latest.get("sign_attempts") or 0) >= 2:
                _halt(latest, "sign_attempt_cap", now=now)
                return {"status": "HALTED", "tree_size": size}
            _safe_event("signer_unavailable", tree_size=size)
            return {"status": "PENDING", "tree_size": size}
    if not auth or not _job_matches_authorized(job, auth):
        _halt(job, "authorized_job_mismatch", now=now)
        return {"status": "HALTED", "tree_size": size}
    if canary.send_state_of(auth) == canary.STATE_AUTHORIZED:
        try:
            fee = int(canary._frozen_policy(auth).get("canonical_fee") or 0)
        except canary.CanaryError:
            fee = 0
        budget_block = _budget_block(fee, now=now)
        if budget_block:
            # Do not burn the single stale-snapshot re-sign while waiting for
            # a UTC budget window to roll. A post-latch exact-txid poll never
            # consumes or rechecks a budget because its POST already happened.
            _safe_event(budget_block, tree_size=size)
            return {"status": "BUDGET_WAIT", "tree_size": size}
    try:
        return _send(
            auth,
            now=now,
            send_fn=send_fn,
            fetch_fn=fetch_fn,
            balance_fetch_fn=balance_fetch_fn,
        )
    except canary.CanaryError as exc:
        message = str(exc)
        state = canary.send_state_of(store.authorized_at(size))
        if state in {
            canary.STATE_SEND_ATTEMPTED,
            canary.STATE_SUBMITTED,
            canary.STATE_CONFIRMED,
        }:
            if "validity window expired" in message and state != canary.STATE_CONFIRMED:
                _halt(job, "ambiguous_send_expired", now=now)
                return {"status": "HALTED", "tree_size": size}
            # CONFIRMED on the authorized row with a lagging public pointer is
            # still a poll-only repair state, never permission to POST again.
            visible = (
                canary.STATE_SUBMITTED
                if state == canary.STATE_CONFIRMED
                else state
            )
            return {"status": visible, "tree_size": size}
        if "stale snapshot" in message and int(job.get("resign_count") or 0) < 1:
            try:
                params = mainnet_params.fetch_trusted_mainnet_params(
                    fetch_fn=params_fetch_fn
                )
                refreshed = store.begin_automation_resign(
                    size,
                    request_id=uuid.uuid4().hex,
                    params=params,
                    authorize_at=now,
                )
                auth = _authorize_job(
                    refreshed, sign_fn=sign_fn, host=host, port=port, now=now
                )
                return _send(
                    auth,
                    now=now,
                    send_fn=send_fn,
                    fetch_fn=fetch_fn,
                    balance_fetch_fn=balance_fetch_fn,
                )
            except Exception:
                latest = store.automation_job_at(size) or job
                latest_auth = store.authorized_at(size) or {}
                latest_state = canary.send_state_of(latest_auth)
                if latest_state in {
                    canary.STATE_SEND_ATTEMPTED,
                    canary.STATE_SUBMITTED,
                }:
                    # The refreshed exact blob may already have crossed the
                    # durable send latch. Treat every later error as an
                    # ambiguous POST and poll only its locally-derived txid.
                    _safe_event("automatic_resign_send_latched", tree_size=size)
                    return {"status": latest_state, "tree_size": size}
                _halt(latest, "automatic_resign_failed", now=now)
                return {"status": "HALTED", "tree_size": size}
        _halt(job, "pre_post_fail_closed", now=now)
        return {"status": "HALTED", "tree_size": size}
    except algo_anchor.AnchorError:
        _safe_event("pre_post_provider_unavailable", tree_size=size)
        return {"status": "PRE_POST_WAIT", "tree_size": size}
    except store.StoreError as exc:
        message = str(exc)
        state = canary.send_state_of(store.authorized_at(size))
        if state in {
            canary.STATE_SEND_ATTEMPTED,
            canary.STATE_SUBMITTED,
            canary.STATE_CONFIRMED,
        }:
            _safe_event("send_race_lost", tree_size=size)
            visible = (
                canary.STATE_SUBMITTED
                if state == canary.STATE_CONFIRMED
                else state
            )
            return {"status": visible, "tree_size": size}
        if "rate cap" in message or "fee cap" in message:
            _safe_event("budget_or_state_gate", tree_size=size)
            return {"status": "BUDGET_WAIT", "tree_size": size}
        latest = store.automation_job_at(size) or job
        _halt(latest, "automatic_store_conflict", now=now)
        return {"status": "HALTED", "tree_size": size}


def tick(
    *,
    now: int | None = None,
    sign_fn=None,
    send_fn=None,
    fetch_fn=None,
    balance_fetch_fn=None,
    params_fetch_fn=None,
    host=None,
    port=None,
) -> dict | None:
    """Run one bounded controller step. Never blocks the route endpoint."""
    when = int(now if now is not None else time.time())
    if not algo_anchor.automatic_mainnet_enabled():
        return None
    if not _lock.acquire(blocking=False):
        return None
    try:
        try:
            _configuration_gate()
        except AutoAnchorError as exc:
            if str(exc) != "disabled":
                _safe_event(str(exc))
            return None
        current = int(store.size() or 0)
        confirmed_row = store.last_confirmed_checkpoint()
        confirmed = int(confirmed_row.get("size") or 0)
        observed = store.automation_observe(
            tree_size=current, confirmed_size=confirmed, now=when
        )
        job = store.last_automation_job()
        if (
            job
            and int(job.get("tree_size") or 0) == confirmed
            and str(job.get("status") or "")
            in {canary.STATE_SEND_ATTEMPTED, canary.STATE_SUBMITTED}
        ):
            auth = store.authorized_at(confirmed) or {}
            expected = str(
                auth.get("expected_txid") or auth.get("txid") or ""
            ).strip()
            if (
                expected
                and str(confirmed_row.get("txid") or "") == expected
                and str(confirmed_row.get("root") or "").lower()
                == str(job.get("root") or "").lower()
            ):
                try:
                    store.set_automatic_send_status(
                        confirmed, canary.STATE_CONFIRMED, now=when
                    )
                    job = store.automation_job_at(confirmed) or job
                except store.StoreError:
                    _safe_event("confirmation_status_reconcile_failed", tree_size=confirmed)
        if job and int(job.get("tree_size") or 0) > confirmed:
            size = int(job.get("tree_size") or 0)
            auth = store.authorized_at(size)
            send_state = canary.send_state_of(auth) if auth else ""
            poll_scope = "poll:%d" % size
            resume_scope = "resume:%d" % size
            if send_state in {
                canary.STATE_SEND_ATTEMPTED,
                canary.STATE_SUBMITTED,
                canary.STATE_CONFIRMED,
            } and not _retry_ready(poll_scope, now=when):
                visible = (
                    canary.STATE_SUBMITTED
                    if send_state == canary.STATE_CONFIRMED
                    else send_state
                )
                return {"status": visible, "tree_size": size}
            if send_state == canary.STATE_AUTHORIZED and not _retry_ready(
                resume_scope, now=when
            ):
                return {"status": canary.STATE_AUTHORIZED, "tree_size": size}
            if str(job.get("status") or "") == "PENDING" and not _retry_ready(
                resume_scope, now=when
            ):
                return {"status": "PENDING", "tree_size": size}
            result = _resume_job(
                job,
                now=when,
                sign_fn=sign_fn,
                send_fn=send_fn,
                fetch_fn=fetch_fn,
                balance_fetch_fn=balance_fetch_fn,
                host=host,
                port=port,
                params_fetch_fn=params_fetch_fn,
            )
            return _track_resume_result(result, tree_size=size, now=when)
        auth = store.last_authorized_checkpoint()
        auth_size = int(auth.get("tree_size") or auth.get("size") or 0)
        if auth.get("signed") and auth_size > confirmed:
            _safe_event("foreign_authorized_hold", tree_size=auth_size)
            return {"status": "HOLD", "tree_size": auth_size}
        delta = max(0, current - confirmed)
        since = int(observed.get("unanchored_since") or 0)
        if not _eligible(delta=delta, unanchored_since=since, now=when):
            return None
        prepare_scope = "prepare:%d" % current
        if not _retry_ready(prepare_scope, now=when):
            return None
        if not _confirmation_path_verified(fetch_fn=fetch_fn):
            _safe_event("confirmation_path_unverified", tree_size=current)
            _defer_retry(prepare_scope, now=when, delay=_PREPARE_RETRY_S)
            return None
        try:
            balance = algo_anchor.fetch_mainnet_account_balance(
                algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME),
                fetch_fn=balance_fetch_fn,
            )
        except algo_anchor.AnchorError:
            _safe_event("balance_unavailable", tree_size=current)
            _defer_retry(prepare_scope, now=when, delay=_PREPARE_RETRY_S)
            return None
        if balance < canary.AUTO_BALANCE_HALT:
            _safe_event("balance_halt", tree_size=current)
            _defer_retry(prepare_scope, now=when, delay=_PREPARE_RETRY_S)
            return None
        if balance < canary.AUTO_BALANCE_WARN:
            _safe_event("balance_warn", tree_size=current)
        try:
            ident = canary.current_checkpoint_identity()
            # Do not persist a job from current_checkpoint_identity's legacy
            # latest-checkpoint fallback. Require the exact frozen size/root
            # checkpoint to be fully published, then re-check it again before
            # signer IPC in _authorize_job.
            ident = canary._automation_checkpoint_identity(
                ident, _capability=canary._AUTO_SEND_CAPABILITY
            )
            params = mainnet_params.fetch_trusted_mainnet_params(
                fetch_fn=params_fetch_fn
            )
            note = algo_anchor.encode_note(
                ident["origin"], ident["tree_size"], ident["root"]
            )
            draft = algo_anchor.build_mainnet_payment_txn(note, params)
            policy = algo_anchor.hmac_policy(
                algo_anchor.fee_policy_snapshot(params, unsigned=draft, now=when)
            )
            budget_block = _budget_block(
                int(policy.get("canonical_fee") or 0), now=when
            )
            if budget_block:
                _safe_event(budget_block, tree_size=current)
                _defer_retry(prepare_scope, now=when, delay=_PREPARE_RETRY_S)
                return None
            job = store.create_automation_job(
                tree_size=int(ident["tree_size"]),
                origin=ident["origin"],
                root=ident["root"],
                checkpoint=ident["checkpoint"],
                request_id=uuid.uuid4().hex,
                params=params,
                authorize_at=when,
            )
        except Exception:
            _safe_event("prepare_fail_closed", tree_size=current)
            _defer_retry(prepare_scope, now=when, delay=_PREPARE_RETRY_S)
            return None
        _clear_retry(prepare_scope)
        return _track_resume_result(
            _resume_job(
                job,
                now=when,
                sign_fn=sign_fn,
                send_fn=send_fn,
                fetch_fn=fetch_fn,
                balance_fetch_fn=balance_fetch_fn,
                host=host,
                port=port,
                params_fetch_fn=params_fetch_fn,
            ),
            tree_size=int(job.get("tree_size") or 0),
            now=when,
        )
    finally:
        _lock.release()


def status(*, now: int | None = None) -> dict:
    when = int(now if now is not None else time.time())
    hour, day, month = canary._budget_windows(when)
    job = store.last_automation_job() or {}
    public_job = {
        "tree_size": int(job.get("tree_size") or 0),
        "status": str(job.get("status") or ""),
        "authorize_at": int(job.get("authorize_at") or 0),
        "resign_count": int(job.get("resign_count") or 0),
        "sign_attempts": int(job.get("sign_attempts") or 0),
        "last_error": str(job.get("last_error") or ""),
        "updated_at": int(job.get("updated_at") or 0),
    }
    return {
        "enabled": algo_anchor.automatic_mainnet_enabled(),
        "killed": algo_anchor.automatic_mainnet_killed(),
        "broadcast_gate": algo_anchor.mainnet_broadcast_requested(),
        "manual_canary_conflict": algo_anchor.mainnet_canary_requested(),
        "observation": store.automation_state(),
        # Omit the checkpoint, policy snapshot, request id, and digests from
        # routine monitoring. They remain in the durable operator record.
        "last_job": public_job,
        "budget": store.automatic_budget_usage(
            hour_start=hour, day_start=day, month_start=month
        ),
        "limits": {
            "delay_s": ANCHOR_SLA_SECONDS,
            "leaves": ANCHOR_SLA_LEAVES,
            "hourly_anchors": canary.AUTO_HOURLY_ANCHOR_MAX,
            "daily_fee": canary.AUTO_DAILY_FEE_MAX,
            "monthly_fee": canary.AUTO_MONTHLY_FEE_MAX,
            "max_fee": canary.AUTO_MAX_FEE,
            "balance_warn": canary.AUTO_BALANCE_WARN,
            "balance_halt": canary.AUTO_BALANCE_HALT,
            "pre_post_resigns": 1,
        },
    }
