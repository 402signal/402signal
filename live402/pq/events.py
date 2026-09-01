"""PQ1 log events. Domain separation is the type field.

402signal.route_decision.v1: commitment-only (nonce + hash). Do not mutate.
402signal.route_decision.v2: 32-byte salt commitment over private evidence.
402signal.route_decision.v3: domain-separated salt commitment over
canonical private evidence (evidence_version 1). Public leaf is type,
minute-rounded ts, nonce, and sha256 commitment only.
402signal.observation_batch.v1: public batch hash and counts.
402signal.scoring_model.v1: public V1 model hash (PR16).

Never put raw need/prompt, payer addresses, salt, or API/merchant bodies on a leaf.
A v2 or v3 public leaf is not a claim of anonymous or unlinkable traffic.
v1 and v2 semantics stay as implemented. New receipts use v3.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from live402.pq import jcs

TYPE_ROUTE_DECISION = "402signal.route_decision.v1"
TYPE_ROUTE_DECISION_V2 = "402signal.route_decision.v2"
TYPE_ROUTE_DECISION_V3 = "402signal.route_decision.v3"
TYPE_OBSERVATION_BATCH = "402signal.observation_batch.v1"
TYPE_SCORING_MODEL = "402signal.scoring_model.v1"
SALT_BYTES = 32
EVIDENCE_VERSION_V3 = 1
V3_DOMAIN = b"402signal.route_decision.v3\0"
# v2 public leaf may include these. Salt, evidence, need, wallet, payment must not.
V2_PUBLIC_FIELDS = frozenset({"type", "ts", "nonce", "commitment", "live", "miss_reason"})
# v3 public leaf is metadata-minimized. Outcome, live, miss_reason, need, url,
# wallet, payment, salt, and private evidence stay off the leaf.
V3_PUBLIC_FIELDS = frozenset({"type", "ts", "nonce", "commitment"})

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


def _str_or_none(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    text = str(val).strip()
    return text or None


def _int_or_none(val) -> int | None:
    if val is None or val is False:
        return None
    if isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _bool_or_none(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and val.strip().lower() in {"unknown", "none", ""}:
        return None
    if val in (0, "0", "false", "False"):
        return False
    if val in (1, "1", "true", "True"):
        return True
    return bool(val)


def _amount_or_none(val):
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return str(val)
    text = str(val).strip()
    return text or None


def _hex64_or_none(val) -> str | None:
    text = _str_or_none(val)
    if text is None:
        return None
    low = text.lower()
    if len(low) != 64:
        return None
    try:
        bytes.fromhex(low)
    except ValueError:
        return None
    return low


def _constraints_object(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            continue
        low = key.lower()
        if low in {f.lower() for f in _FORBIDDEN} or "prompt" in low or low == "need":
            continue
        if key in ("wallet", "wallets", "PAYMENT-SIGNATURE", "X-PAYMENT", "authorization"):
            continue
        out[key] = val
    return out


def candidate_set_digest(compared) -> str | None:
    """Canonical private digest of compared candidates. Not written to the public leaf.

    Bound per row when present: url, rail, live, invocable, selected,
    amount_atomic, latency_ms, and selected_payment identity
    (rail/network/scheme/asset/amount_atomic/payTo). Catalog claims,
    seller bodies, raw PAYMENT, and full compared[] dumps are not bound.
    """
    if not isinstance(compared, list) or not compared:
        return None
    slim = []
    for row in compared:
        if not isinstance(row, dict):
            continue
        pay = row.get("selected_payment") if isinstance(row.get("selected_payment"), dict) else {}
        amount = row.get("amount_atomic") if row.get("amount_atomic") is not None else pay.get("amount_atomic")
        item = {
            "amount_atomic": _amount_or_none(amount),
            "invocable": _bool_or_none(row.get("invocable")),
            "latency_ms": _int_or_none(row.get("latency_ms")),
            "live": _bool_or_none(row.get("live")),
            "rail": _str_or_none(row.get("rail") or pay.get("rail")),
            "selected": _bool_or_none(row.get("selected")),
            "selected_payment": None,
            "url": _str_or_none(row.get("url")),
        }
        if pay:
            item["selected_payment"] = {
                "amount_atomic": _amount_or_none(pay.get("amount_atomic")),
                "asset": _str_or_none(pay.get("asset")),
                "network": _str_or_none(pay.get("network")),
                "payTo": _str_or_none(pay.get("payTo")),
                "rail": _str_or_none(pay.get("rail")),
                "scheme": _str_or_none(pay.get("scheme")),
            }
        slim.append(item)
    if not slim:
        return None
    slim.sort(key=lambda r: (r.get("url") or "", r.get("rail") or "", r.get("selected") is True))
    return _sha256_hex(jcs.canonicalize(slim))


def canonicalize_private_evidence_v3(raw: dict) -> dict:
    """Deterministic evidence_version 1 object. Missing facts are null, never invented."""
    if not isinstance(raw, dict):
        raise PrivacyError("v3 evidence must be an object")
    ver = raw.get("evidence_version")
    if ver not in (EVIDENCE_VERSION_V3, str(EVIDENCE_VERSION_V3)):
        raise PrivacyError("v3 evidence_version must be 1")
    req = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    pol = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    dec = raw.get("decision") if isinstance(raw.get("decision"), dict) else {}
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    pay = raw.get("selected_payment") if isinstance(raw.get("selected_payment"), dict) else None
    cmp_ = raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {}
    sco = raw.get("scoring") if isinstance(raw.get("scoring"), dict) else {}
    unresolved = pol.get("unresolved")
    if not isinstance(unresolved, list):
        unresolved = []
    unresolved = [str(x) for x in unresolved if x is not None and str(x).strip()]
    selected = None
    if pay:
        selected = {
            "amount_atomic": _amount_or_none(pay.get("amount_atomic")),
            "asset": _str_or_none(pay.get("asset")),
            "network": _str_or_none(pay.get("network")),
            "payTo": _str_or_none(pay.get("payTo")),
            "rail": _str_or_none(pay.get("rail")),
            "scheme": _str_or_none(pay.get("scheme")),
        }
    evidence = {
        "comparison": {
            "candidate_count": _int_or_none(cmp_.get("candidate_count")),
            "candidate_set_digest": _hex64_or_none(cmp_.get("candidate_set_digest")),
            "observation_batch_hash": _hex64_or_none(cmp_.get("observation_batch_hash")),
            "probe_batch_id": _str_or_none(cmp_.get("probe_batch_id")),
        },
        "decision": {
            "miss_reason": _str_or_none(dec.get("miss_reason")),
            "outcome": _str_or_none(dec.get("outcome")),
            "winner_url": _str_or_none(dec.get("winner_url")),
        },
        "evidence_version": EVIDENCE_VERSION_V3,
        "observation": {
            "challenge_observed": _bool_or_none(obs.get("challenge_observed")),
            "http_status": _int_or_none(obs.get("http_status")),
            "invocable": _bool_or_none(obs.get("invocable")),
            "latency_ms": _int_or_none(obs.get("latency_ms")),
            "live": _bool_or_none(obs.get("live")),
            "observed_at": _str_or_none(obs.get("observed_at")),
            "payable": _bool_or_none(obs.get("payable")),
        },
        "policy": {
            "constraints": _constraints_object(pol.get("constraints")),
            "objective": _str_or_none(pol.get("objective")),
            "unresolved": unresolved,
        },
        "request": {
            "need": _str_or_none(req.get("need")),
            "url": _str_or_none(req.get("url")),
        },
        "scoring": {
            "model_hash": _hex64_or_none(sco.get("model_hash")),
            "model_id": _str_or_none(sco.get("model_id")),
        },
        "selected_payment": selected,
    }
    return evidence


def private_evidence_v3_from_route(result: dict | None, request: dict | None = None) -> dict:
    """Build evidence_version 1 from a /route result. Catalog claims are not observed."""
    res = result if isinstance(result, dict) else {}
    req = request if isinstance(request, dict) else {}
    observed = res.get("observed") if isinstance(res.get("observed"), dict) else {}
    selected = res.get("selected_payment") if isinstance(res.get("selected_payment"), dict) else None
    compared = res.get("compared") if isinstance(res.get("compared"), list) else None
    applied = res.get("applied_constraints") if isinstance(res.get("applied_constraints"), dict) else None
    if applied is None:
        applied = res.get("interpreted_constraints") if isinstance(res.get("interpreted_constraints"), dict) else {}
    unresolved = res.get("unresolved_constraints")
    if not isinstance(unresolved, list):
        unresolved = []
    req_url = req.get("url") if isinstance(req.get("url"), str) else None
    res_url = res.get("url") if isinstance(res.get("url"), str) else None
    winner_url = res_url or req_url
    live_obs = observed.get("live") if "live" in observed else None
    if live_obs is None:
        if observed.get("http_status") == 402 or res.get("challenge_observed") or res.get("status") == 402:
            live_obs = True
        elif "live" in res:
            live_obs = res.get("live")
    payable_obs = observed.get("payable") if "payable" in observed else res.get("payable")
    invocable_obs = observed.get("invocable") if "invocable" in observed else res.get("invocable")
    http_status = observed.get("http_status") if observed.get("http_status") is not None else res.get("status")
    latency = observed.get("latency_ms") if observed.get("latency_ms") is not None else res.get("latency_ms")
    observed_at = observed.get("observed_at") or res.get("probed_at") or res.get("verified_at")
    challenge = res.get("challenge_observed")
    if challenge is None and (http_status == 402 or res.get("has_402_challenge")):
        challenge = True
    miss = res.get("miss_reason") if isinstance(res.get("miss_reason"), str) else None
    if selected and res.get("live"):
        outcome = "winner"
    elif selected:
        outcome = "winner"
    else:
        outcome = "miss"
    digest = candidate_set_digest(compared)
    candidate_count = len(compared) if isinstance(compared, list) else None
    if candidate_count is None:
        candidate_count = _int_or_none(res.get("candidates_probed") or res.get("probed_count"))
    batch_id = _str_or_none(res.get("batch_id"))
    batch_hash = _hex64_or_none(res.get("observation_batch_hash"))
    if batch_hash is None and batch_id:
        try:
            from live402 import history as history_mod

            att = history_mod.attestation_for(batch_id)
            if isinstance(att, dict):
                batch_hash = _hex64_or_none(att.get("hash"))
        except Exception:
            batch_hash = None
    model_id = None
    model_hash = None
    scoring = res.get("scoring_model") if isinstance(res.get("scoring_model"), dict) else None
    rep = res.get("reputation") if isinstance(res.get("reputation"), dict) else {}
    if scoring:
        model_id = scoring.get("model_id")
        model_hash = scoring.get("model_hash")
    if model_id is None:
        model_id = res.get("scoring_model_id") or rep.get("scoring_model_id")
    if model_hash is None:
        model_hash = res.get("scoring_model_hash") or rep.get("scoring_model_hash")
    if model_id is None or model_hash is None:
        try:
            from live402 import reputation as reputation_mod

            rec = reputation_mod.model_record()
            model_id = model_id or rec.get("model_id")
            model_hash = model_hash or rec.get("model_hash")
        except Exception:
            pass
    raw = {
        "comparison": {
            "candidate_count": candidate_count,
            "candidate_set_digest": digest,
            "observation_batch_hash": batch_hash,
            "probe_batch_id": batch_id,
        },
        "decision": {
            "miss_reason": miss,
            "outcome": outcome,
            "winner_url": winner_url,
        },
        "evidence_version": EVIDENCE_VERSION_V3,
        "observation": {
            "challenge_observed": challenge,
            "http_status": http_status,
            "invocable": invocable_obs,
            "latency_ms": latency,
            "live": live_obs,
            "observed_at": observed_at,
            "payable": payable_obs,
        },
        "policy": {
            "constraints": applied,
            "objective": res.get("objective") or req.get("objective"),
            "unresolved": unresolved,
        },
        "request": {
            "need": req.get("need") if isinstance(req.get("need"), str) else res.get("need"),
            "url": req_url or res_url,
        },
        "scoring": {
            "model_hash": model_hash,
            "model_id": model_id,
        },
        "selected_payment": selected,
    }
    return canonicalize_private_evidence_v3(raw)


def commitment_hash_v3(evidence: dict, salt: bytes) -> str:
    """SHA-256(domain || JCS(evidence) || salt). Domain is route_decision.v3 + NUL."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise PrivacyError("v3 salt must be 32 bytes")
    ev = canonicalize_private_evidence_v3(evidence)
    digest = hashlib.sha256()
    digest.update(V3_DOMAIN)
    digest.update(jcs.canonicalize(ev))
    digest.update(bytes(salt))
    return digest.hexdigest()


def reveal_bundle_v3(evidence: dict, salt: bytes, *, ts: str, nonce: str, commitment: str) -> dict:
    """Customer-only reveal. Public log gets neither evidence nor salt."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise PrivacyError("v3 salt must be 32 bytes")
    ev = canonicalize_private_evidence_v3(evidence)
    return {
        "commitment": commitment,
        "event_version": TYPE_ROUTE_DECISION_V3,
        "evidence": ev,
        "nonce": nonce,
        "salt": bytes(salt).hex(),
        "ts": ts,
    }


def verify_reveal_v3(commitment: str, reveal: dict) -> bool:
    """True when the customer reveal recomputes the public v3 commitment. Fail closed."""
    if not isinstance(reveal, dict) or not isinstance(commitment, str) or len(commitment) != 64:
        return False
    version = reveal.get("event_version") or reveal.get("type")
    if version != TYPE_ROUTE_DECISION_V3:
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
    expected = reveal.get("commitment")
    if expected is not None and not isinstance(expected, str):
        return False
    if isinstance(expected, str) and expected.lower() != commitment.lower():
        return False
    try:
        recomputed = commitment_hash_v3(evidence, salt)
    except (PrivacyError, jcs.JCSError, ValueError, TypeError):
        return False
    return recomputed == commitment.lower()


def route_decision_event_v3(
    *,
    evidence: dict,
    ts: int | None = None,
    nonce: str | None = None,
    salt: bytes | None = None,
) -> tuple[dict, dict]:
    """Return (public_leaf, customer_reveal). Public leaf has no salt or evidence."""
    salt_b = bytes(salt) if salt is not None else new_salt()
    if len(salt_b) != SALT_BYTES:
        raise PrivacyError("v3 salt must be 32 bytes")
    ev = canonicalize_private_evidence_v3(evidence)
    when = jcs.utc_minutes_z(ts if ts is not None else None)
    nonce_s = nonce or secrets.token_hex(32)
    commitment = commitment_hash_v3(ev, salt_b)
    event = {
        "commitment": commitment,
        "nonce": nonce_s,
        "ts": when,
        "type": TYPE_ROUTE_DECISION_V3,
    }
    return assert_public(event), reveal_bundle_v3(ev, salt_b, ts=when, nonce=nonce_s, commitment=commitment)


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
        TYPE_ROUTE_DECISION_V3,
        TYPE_OBSERVATION_BATCH,
        TYPE_SCORING_MODEL,
    }:
        raise PrivacyError("unknown event type")
    jcs.require_timestamp(event.get("ts") or "")
    _forbid(event)
    if typ in {TYPE_ROUTE_DECISION, TYPE_ROUTE_DECISION_V2, TYPE_ROUTE_DECISION_V3}:
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
        if typ == TYPE_ROUTE_DECISION_V3:
            extra = set(event) - V3_PUBLIC_FIELDS
            if extra:
                raise PrivacyError("v3 public leaf has forbidden field")
            if "salt" in event or "evidence" in event:
                raise PrivacyError("v3 public leaf must not include salt or evidence")
            if event.get("live") is not None or event.get("miss_reason") is not None:
                raise PrivacyError("v3 public leaf must not include outcome fields")
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
