"""PQ1 log events. Domain separation is the type field.

402signal.route_decision.v1 — commitment-only (nonce + hash).
402signal.observation_batch.v1 — public batch hash and counts.
402signal.scoring_model.v1 — public V1 model hash (PR16).

Never put raw need/prompt, payer addresses, or API/merchant bodies on a leaf.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from live402.pq import jcs

TYPE_ROUTE_DECISION = "402signal.route_decision.v1"
TYPE_OBSERVATION_BATCH = "402signal.observation_batch.v1"
TYPE_SCORING_MODEL = "402signal.scoring_model.v1"

_FORBIDDEN = (
    "need",
    "prompt",
    "wallet",
    "wallets",
    "payer",
    "payers",
    "payer_addresses",
    "unique_payer_addresses",
    "payTo",
    "address",
    "addresses",
    "body",
    "api_body",
    "request_body",
    "response_body",
    "merchant_body",
    "PAYMENT-SIGNATURE",
    "X-PAYMENT",
)


class PrivacyError(ValueError):
    pass


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def commitment_hash(*, need: str = "", url: str = "", prompt: str = "", extra: dict | None = None) -> str:
    """Hash private request material. The hash may be logged; the inputs must not."""
    payload = {
        "extra": extra if isinstance(extra, dict) else {},
        "need": need or "",
        "prompt": prompt or "",
        "url": url or "",
    }
    return _sha256_hex(jcs.canonicalize(payload))


def route_decision_event(
    *,
    need: str = "",
    url: str = "",
    prompt: str = "",
    live: bool | None = None,
    miss_reason: str | None = None,
    ts: int | None = None,
    nonce: str | None = None,
) -> dict:
    when = jcs.utc_seconds_z(ts if ts is not None else int(time.time()))
    event = {
        "commitment": commitment_hash(need=need, url=url, prompt=prompt),
        "live": bool(live) if live is not None else None,
        "miss_reason": miss_reason or None,
        "nonce": nonce or secrets.token_hex(32),
        "ts": when,
        "type": TYPE_ROUTE_DECISION,
    }
    if event["live"] is None:
        event.pop("live")
    if event["miss_reason"] is None:
        event.pop("miss_reason")
    return assert_public(event)


def observation_batch_event(
    *,
    batch_id: str,
    n: int,
    digest: str,
    counts: dict | None = None,
    ts: int | None = None,
) -> dict:
    when = jcs.utc_seconds_z(ts if ts is not None else int(time.time()))
    event = {
        "batch_id": batch_id,
        "counts": counts if isinstance(counts, dict) else {},
        "hash": digest,
        "n": int(n),
        "ts": when,
        "type": TYPE_OBSERVATION_BATCH,
    }
    return assert_public(jcs.amounts_as_strings(event))


def scoring_model_event(record: dict | None = None, ts: int | None = None) -> dict:
    rec = record if isinstance(record, dict) else {}
    if not rec:
        from live402 import reputation

        rec = reputation.model_record()
    when = jcs.utc_seconds_z(ts if ts is not None else int(rec.get("effective_ts") or time.time()))
    event = {
        "effective_ts": when,
        "model_hash": rec.get("model_hash"),
        "model_id": rec.get("model_id"),
        "ts": when,
        "type": TYPE_SCORING_MODEL,
    }
    return assert_public(event)


def assert_public(event: dict) -> dict:
    if not isinstance(event, dict):
        raise PrivacyError("event must be an object")
    typ = event.get("type")
    if typ not in {TYPE_ROUTE_DECISION, TYPE_OBSERVATION_BATCH, TYPE_SCORING_MODEL}:
        raise PrivacyError("unknown event type")
    jcs.require_timestamp(event.get("ts") or "")
    _forbid(event)
    if typ == TYPE_ROUTE_DECISION:
        nonce = event.get("nonce") or ""
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise PrivacyError("route_decision nonce must be high entropy")
        if not event.get("commitment"):
            raise PrivacyError("route_decision requires a commitment hash")
    return event


def _forbid(obj, path: str = "") -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            low = str(key)
            if low in _FORBIDDEN or low.lower() in {f.lower() for f in _FORBIDDEN}:
                raise PrivacyError("forbidden field %s" % key)
            if "prompt" in low.lower() or low.lower() == "need":
                raise PrivacyError("forbidden field %s" % key)
            _forbid(val, path + "." + str(key))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            _forbid(val, path + "[%s]" % i)


def leaf_bytes(event: dict) -> bytes:
    """JCS bytes that become the RFC 9162 leaf entry. Type is the domain separator."""
    clean = assert_public(jcs.amounts_as_strings(event))
    return jcs.canonicalize(clean)
