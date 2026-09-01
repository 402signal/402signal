"""PQ1 log events. Domain separation is the type field.

402signal.route_decision.v1 — commitment-only (nonce + hash). Do not mutate.
402signal.route_decision.v2 — 32-byte salt commitment over private evidence.
402signal.observation_batch.v1 — public batch hash and counts.
402signal.scoring_model.v1 — public V1 model hash (PR16).

Never put raw need/prompt, payer addresses, salt, or API/merchant bodies on a leaf.
A v2 public leaf is not a claim of anonymous or unlinkable traffic.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from live402.pq import jcs

TYPE_ROUTE_DECISION = "402signal.route_decision.v1"
TYPE_ROUTE_DECISION_V2 = "402signal.route_decision.v2"
TYPE_OBSERVATION_BATCH = "402signal.observation_batch.v1"
TYPE_SCORING_MODEL = "402signal.scoring_model.v1"
SALT_BYTES = 32
# v2 public leaf may include these. Salt, evidence, need, wallet, payment must not.
V2_PUBLIC_FIELDS = frozenset({"type", "ts", "nonce", "commitment", "live", "miss_reason"})

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
    """v1 hash of private request material. The hash may be logged; the inputs must not."""
    payload = {
        "extra": extra if isinstance(extra, dict) else {},
        "need": need or "",
        "prompt": prompt or "",
        "url": url or "",
    }
    return _sha256_hex(jcs.canonicalize(payload))


def private_evidence(
    *,
    need: str = "",
    url: str = "",
    prompt: str = "",
    extra: dict | None = None,
) -> dict:
    """Private evidence envelope. Never written to a public leaf."""
    return {
        "extra": extra if isinstance(extra, dict) else {},
        "need": need or "",
        "prompt": prompt or "",
        "url": url or "",
    }


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def commitment_hash_v2(evidence: dict, salt: bytes) -> str:
    """SHA-256 of JCS({evidence, salt_hex}). Customer reveal recomputes this."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise PrivacyError("v2 salt must be 32 bytes")
    if not isinstance(evidence, dict):
        raise PrivacyError("v2 evidence must be an object")
    payload = {
        "evidence": private_evidence(
            need=evidence.get("need") if isinstance(evidence.get("need"), str) else "",
            url=evidence.get("url") if isinstance(evidence.get("url"), str) else "",
            prompt=evidence.get("prompt") if isinstance(evidence.get("prompt"), str) else "",
            extra=evidence.get("extra") if isinstance(evidence.get("extra"), dict) else {},
        ),
        "salt": bytes(salt).hex(),
    }
    return _sha256_hex(jcs.canonicalize(payload))


def reveal_bundle(evidence: dict, salt: bytes) -> dict:
    """Customer-only reveal. Recompute commitment_hash_v2(evidence, salt)."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise PrivacyError("v2 salt must be 32 bytes")
    ev = private_evidence(
        need=evidence.get("need") if isinstance(evidence.get("need"), str) else "",
        url=evidence.get("url") if isinstance(evidence.get("url"), str) else "",
        prompt=evidence.get("prompt") if isinstance(evidence.get("prompt"), str) else "",
        extra=evidence.get("extra") if isinstance(evidence.get("extra"), dict) else {},
    )
    return {
        "commitment": commitment_hash_v2(ev, bytes(salt)),
        "evidence": ev,
        "salt": bytes(salt).hex(),
    }


def verify_reveal(commitment: str, reveal: dict) -> bool:
    """True when the customer reveal recomputes the public commitment."""
    if not isinstance(reveal, dict) or not isinstance(commitment, str) or len(commitment) != 64:
        return False
    try:
        salt = bytes.fromhex(str(reveal.get("salt") or ""))
    except ValueError:
        return False
    if len(salt) != SALT_BYTES:
        return False
    evidence = reveal.get("evidence")
    if not isinstance(evidence, dict):
        return False
    try:
        return commitment_hash_v2(evidence, salt) == commitment.lower()
    except PrivacyError:
        return False


def route_decision_event_v2(
    *,
    need: str = "",
    url: str = "",
    prompt: str = "",
    extra: dict | None = None,
    live: bool | None = None,
    miss_reason: str | None = None,
    ts: int | None = None,
    nonce: str | None = None,
    salt: bytes | None = None,
) -> tuple[dict, dict]:
    """Return (public_leaf, customer_reveal). Public leaf has no salt or evidence."""
    salt_b = bytes(salt) if salt is not None else new_salt()
    if len(salt_b) != SALT_BYTES:
        raise PrivacyError("v2 salt must be 32 bytes")
    evidence = private_evidence(need=need, url=url, prompt=prompt, extra=extra)
    when = jcs.utc_seconds_z(ts if ts is not None else int(time.time()))
    event = {
        "commitment": commitment_hash_v2(evidence, salt_b),
        "live": bool(live) if live is not None else None,
        "miss_reason": miss_reason or None,
        "nonce": nonce or secrets.token_hex(32),
        "ts": when,
        "type": TYPE_ROUTE_DECISION_V2,
    }
    if event["live"] is None:
        event.pop("live")
    if event["miss_reason"] is None:
        event.pop("miss_reason")
    return assert_public(event), reveal_bundle(evidence, salt_b)


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
    if typ not in {
        TYPE_ROUTE_DECISION,
        TYPE_ROUTE_DECISION_V2,
        TYPE_OBSERVATION_BATCH,
        TYPE_SCORING_MODEL,
    }:
        raise PrivacyError("unknown event type")
    jcs.require_timestamp(event.get("ts") or "")
    _forbid(event)
    if typ in {TYPE_ROUTE_DECISION, TYPE_ROUTE_DECISION_V2}:
        nonce = event.get("nonce") or ""
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise PrivacyError("route_decision nonce must be high entropy")
        if not event.get("commitment"):
            raise PrivacyError("route_decision requires a commitment hash")
        if typ == TYPE_ROUTE_DECISION_V2:
            extra = set(event) - V2_PUBLIC_FIELDS
            if extra:
                raise PrivacyError("v2 public leaf has forbidden field")
            if "salt" in event or "evidence" in event:
                raise PrivacyError("v2 public leaf must not include salt or evidence")
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
